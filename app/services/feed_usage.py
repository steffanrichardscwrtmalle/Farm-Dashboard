"""Import and display Feedlync Loaded Mixes → By Ingredient usage."""

from __future__ import annotations

import datetime as dt
import io
import threading
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, FeedUsageRecord
from app.services.farm_schedule import FARM_LABELS, normalize_farm
from app.services.feed_usage_settings import (
    assigned_ration_names,
    ingredient_inclusion_lookup,
    is_usage_ingredient_included,
    seed_ration_assignments_if_empty,
)
from app.services.feedlync_api import (
    USAGE_RATIONS_BY_FARM,
    fetch_loaded_mix_ingredient_usage,
    month_bounds,
    previous_calendar_month,
)
from app.services.feedlync_auth import FeedlyncAuthError

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
USAGE_COMBINED_FARM = "COMBINED"
USAGE_FARM_LABELS: dict[str, str] = {
    **FARM_LABELS,
    USAGE_COMBINED_FARM: "Combined",
}
_USAGE_XLSX_HEADERS = ("Ingredient", "As Fed (kg)", "As Fed (MT)", "Avg MT/day")
_THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
_KG_NUMBER_FORMAT = "#,##0"
_MT_NUMBER_FORMAT = "#,##0.0000"

_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "latest_import": None,
    "rows_imported": 0,
    "month": None,
    "needs_auth": False,
}


def get_import_status() -> dict[str, Any]:
    with _lock:
        return dict(_import_status)


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _import_status.update(kwargs)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started(month: str) -> None:
    _set_status(
        status="running",
        message="Starting Feedlync usage import…",
        rows_imported=0,
        month=month,
        needs_auth=False,
    )


def normalize_usage_farm(farm: str | None) -> str:
    value = (farm or "").strip().upper()
    if value == USAGE_COMBINED_FARM:
        return USAGE_COMBINED_FARM
    return normalize_farm(value)


def usage_farm_label(farm: str) -> str:
    return USAGE_FARM_LABELS[normalize_usage_farm(farm)]


def resolve_usage_month(value: str | None, *, today: dt.date | None = None) -> dt.date:
    if value:
        text = value.strip()
        if len(text) == 7 and text[4] == "-":
            text = text + "-01"
        try:
            parsed = dt.date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid month: {value}") from exc
        return parsed.replace(day=1)
    return previous_calendar_month(today)


def previous_month_usage_import_month(today: dt.date) -> dt.date | None:
    """On the 2nd of the month, return the first day of the previous month."""
    if today.day != 2:
        return None
    return previous_calendar_month(today)


def import_previous_month_usage_if_due(
    db: Session, *, today: dt.date
) -> dict[str, Any] | None:
    month = previous_month_usage_import_month(today)
    if month is None:
        return None
    return import_feed_usage(db, month=month)


def _round_kg(value: float) -> float:
    return round(value, 2)


def _round_money(value: float) -> float:
    return round(value, 2)


def _default_usage_rations(farm_key: str) -> list[str]:
    if farm_key == USAGE_COMBINED_FARM:
        names: list[str] = []
        for farm in HERD_FARM_OPTIONS:
            for name in USAGE_RATIONS_BY_FARM.get(farm, ()):
                if name not in names:
                    names.append(name)
        return names
    return list(USAGE_RATIONS_BY_FARM.get(farm_key, ()))


def build_usage_report(
    rows: list[dict[str, Any]],
    *,
    period_start: dt.date,
    period_end: dt.date,
    farm: str,
    rations: list[str] | None = None,
    import_timestamp: dt.datetime | None = None,
) -> dict[str, Any]:
    farm_key = normalize_usage_farm(farm)
    ration_names = list(rations) if rations is not None else _default_usage_rations(farm_key)
    days = (period_end - period_start).days + 1
    ingredients = sorted(
        rows,
        key=lambda row: (row.get("ingredient_name") or "").casefold(),
    )
    total_as_fed = sum(float(row.get("as_fed_kg") or 0) for row in ingredients)
    total_dm = sum(float(row.get("dm_kg") or 0) for row in ingredients)
    total_cost = sum(float(row.get("cost") or 0) for row in ingredients)

    def ingredient_row(row: dict[str, Any]) -> dict[str, Any]:
        kg = float(row.get("as_fed_kg") or 0)
        return {
            "ingredient_name": row.get("ingredient_name") or "",
            "as_fed_kg": _round_kg(kg),
            "as_fed_mt_per_day": _mt_per_day(kg, days),
            "dm_kg": _round_kg(float(row.get("dm_kg") or 0)),
            "cost": _round_money(float(row.get("cost") or 0)),
        }

    return {
        "month": period_start.strftime("%Y-%m"),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "days": days,
        "farm": farm_key,
        "farm_label": USAGE_FARM_LABELS[farm_key],
        "rations": ration_names,
        "source": "Loaded Mixes → By Ingredient",
        "ingredients": [ingredient_row(row) for row in ingredients],
        "totals": {
            "as_fed_kg": _round_kg(total_as_fed),
            "as_fed_mt_per_day": _mt_per_day(total_as_fed, days),
            "dm_kg": _round_kg(total_dm),
            "cost": _round_money(total_cost),
        },
        "averages": {
            "as_fed_kg": _round_kg(total_as_fed / days) if days else 0.0,
            "dm_kg": _round_kg(total_dm / days) if days else 0.0,
            "cost": _round_money(total_cost / days) if days else 0.0,
        },
        "latest_import": import_timestamp.isoformat() if import_timestamp else None,
        "row_count": len(ingredients),
    }


def _as_fed_mt(kg: float) -> float:
    return round(float(kg or 0) / 1000, 4)


def _mt_per_day(kg: float, days: int) -> float:
    if not days:
        return 0.0
    return round((float(kg or 0) / 1000) / days, 4)


def _xlsx_display_width(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return len(str(value))
    if isinstance(value, int):
        return len(f"{value:,}")
    if isinstance(value, float):
        return len(f"{value:,.4f}".rstrip("0").rstrip("."))
    return len(str(value))


def _fit_xlsx_columns(ws) -> None:
    for index in range(1, (ws.max_column or 1) + 1):
        letter = get_column_letter(index)
        widest = 0
        for cell in ws[letter]:
            widest = max(widest, _xlsx_display_width(cell.value))
        ws.column_dimensions[letter].width = min(max(widest + 2, 12), 48)


def _border_xlsx_cells(ws) -> None:
    for row in ws.iter_rows(
        min_row=1,
        max_row=max(ws.max_row, 1),
        min_col=1,
        max_col=max(ws.max_column, 1),
    ):
        for cell in row:
            cell.border = _THIN_BORDER


def build_usage_xlsx(report: dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    farm_label = str(report.get("farm_label") or report.get("farm") or "Feed Usage")
    ws.title = farm_label[:31]
    header_font = Font(bold=True)
    total_font = Font(bold=True)
    days = int(report.get("days") or 0)
    ws.append(list(_USAGE_XLSX_HEADERS))
    for cell in ws[1]:
        cell.font = header_font
    for column in ("B", "C", "D"):
        ws[f"{column}1"].alignment = Alignment(horizontal="right")

    def _write_usage_row(name: str, kg: float, *, bold: bool = False) -> None:
        ws.append([name, kg, _as_fed_mt(kg), _mt_per_day(kg, days)])
        data_row = ws[ws.max_row]
        if bold:
            for cell in data_row:
                cell.font = total_font
        data_row[1].number_format = _KG_NUMBER_FORMAT
        data_row[2].number_format = _MT_NUMBER_FORMAT
        data_row[3].number_format = _MT_NUMBER_FORMAT
        for cell in data_row[1:]:
            cell.alignment = Alignment(horizontal="right", vertical="center")

    for row in report.get("ingredients") or []:
        _write_usage_row(row.get("ingredient_name") or "", float(row.get("as_fed_kg") or 0))

    totals = report.get("totals") or {}
    _write_usage_row("Total", float(totals.get("as_fed_kg") or 0), bold=True)

    _fit_xlsx_columns(ws)
    _border_xlsx_cells(ws)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _combined_usage_rows(records: list[FeedUsageRecord]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        name = (record.ingredient_name or "").strip()
        bucket = merged.get(name)
        if bucket is None:
            bucket = {
                "ingredient_name": name,
                "as_fed_kg": 0.0,
                "dm_kg": 0.0,
                "cost": 0.0,
            }
            merged[name] = bucket
        bucket["as_fed_kg"] += float(record.as_fed_kg or 0)
        bucket["dm_kg"] += float(record.dm_kg or 0)
        bucket["cost"] += float(record.cost or 0)
    return list(merged.values())


def fiscal_year_for_date(value: dt.date) -> int:
    """UK fiscal year ending in March. April 2026 is FY 2027."""
    return value.year + 1 if value.month >= 4 else value.year


def fiscal_year_bounds(fiscal_year: int) -> tuple[dt.date, dt.date]:
    return dt.date(fiscal_year - 1, 4, 1), dt.date(fiscal_year, 3, 31)


def usage_month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    cursor = start.replace(day=1)
    last = end.replace(day=1)
    months: list[dt.date] = []
    while cursor <= last:
        months.append(cursor)
        if cursor.month == 12:
            cursor = dt.date(cursor.year + 1, 1, 1)
        else:
            cursor = dt.date(cursor.year, cursor.month + 1, 1)
    return months


def parse_usage_month_value(value: str) -> dt.date:
    text = value.strip()
    if len(text) == 7 and text[4] == "-":
        text = text + "-01"
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid month: {value}") from exc
    return parsed.replace(day=1)


def default_usage_month_for_fiscal_year(fiscal_year: int, today: dt.date) -> dt.date:
    """Previous month when it sits in this fiscal year, otherwise the latest month so far."""
    previous = previous_calendar_month(today)
    start, end = fiscal_year_bounds(fiscal_year)
    if start <= previous <= end:
        return previous
    if end < today:
        return dt.date(fiscal_year, 3, 1)
    if today < start:
        return start
    return today.replace(day=1)


def resolve_usage_period(
    *,
    fiscal_year: str | None = None,
    month: str | None = None,
    month_from: str | None = None,
    month_to: str | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Resolve the feed usage period. Fiscal year defaults to the current year."""
    current_day = today or dt.date.today()
    current_fy = fiscal_year_for_date(current_day)
    fy_text = (fiscal_year or "").strip().lower()
    any_year = fy_text == "any"
    fiscal_year_num: int | None
    if not fy_text:
        fiscal_year_num = None
    elif any_year:
        fiscal_year_num = None
    else:
        try:
            fiscal_year_num = int(fy_text)
        except ValueError as exc:
            raise ValueError("Fiscal year must be a year or Any.") from exc
        if fiscal_year_num < 2000 or fiscal_year_num > 2100:
            raise ValueError("Fiscal year is out of range.")

    month_text = (month or "").strip().lower()
    is_range = month_text == "range"
    if is_range:
        if not (month_from or "").strip() or not (month_to or "").strip():
            raise ValueError("Choose a from and to month for a range.")
        start = parse_usage_month_value(month_from or "")
        end = parse_usage_month_value(month_to or "")
    elif month_text:
        start = end = parse_usage_month_value(month_text)
    elif fiscal_year_num is None and not any_year:
        fiscal_year_num = current_fy
        start = end = default_usage_month_for_fiscal_year(current_fy, current_day)
    elif any_year:
        start = end = default_usage_month_for_fiscal_year(current_fy, current_day)
    else:
        start = end = default_usage_month_for_fiscal_year(fiscal_year_num or current_fy, current_day)

    if end < start:
        raise ValueError("The end month is before the start month.")

    if fiscal_year_num is None and not any_year:
        fiscal_year_num = fiscal_year_for_date(start)
    if not any_year and fiscal_year_num is not None:
        fy_start, fy_end = fiscal_year_bounds(fiscal_year_num)
        if start < fy_start or end > fy_end:
            raise ValueError("Months must fall inside the selected fiscal year.")

    months = usage_month_starts(start, end)
    if len(months) > 36:
        raise ValueError("Choose a range of 36 months or fewer.")

    return {
        "fiscal_year": None if any_year else fiscal_year_num,
        "any_year": any_year,
        "is_range": is_range or start != end,
        "month_from": start,
        "month_to": end,
        "months": months,
    }


def usage_fiscal_year_options(db: Session, *, today: dt.date | None = None) -> list[int]:
    current = fiscal_year_for_date(today or dt.date.today())
    years = {current, current - 1}
    starts = db.scalars(select(FeedUsageRecord.period_start).distinct()).all()
    for start in starts:
        if start is not None:
            years.add(fiscal_year_for_date(start))
    return sorted(years, reverse=True)


def _usage_ration_names(db: Session, farm_key: str) -> list[str]:
    if farm_key != USAGE_COMBINED_FARM:
        return assigned_ration_names(db, farm_key)
    names: list[str] = []
    for farm in HERD_FARM_OPTIONS:
        for name in assigned_ration_names(db, farm):
            if name not in names:
                names.append(name)
    return names


def usage_import_is_partial(period_end: dt.date, imported_on: dt.date) -> bool:
    """A month fetched before it had finished is only a partial snapshot."""
    return period_end >= imported_on


def discard_expired_partial_usage(db: Session, *, today: dt.date | None = None) -> int:
    """Drop an in-progress month once the day it was fetched has passed."""
    current_day = today or dt.date.today()
    expired_ids = [
        record.id
        for record in db.scalars(select(FeedUsageRecord)).all()
        if record.import_timestamp
        and usage_import_is_partial(record.period_end, record.import_timestamp.date())
        and record.import_timestamp.date() < current_day
    ]
    if not expired_ids:
        return 0
    db.execute(delete(FeedUsageRecord).where(FeedUsageRecord.id.in_(expired_ids)))
    db.commit()
    return len(expired_ids)


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _month_label(value: dt.date) -> str:
    return f"{_MONTH_NAMES[value.month - 1]} {value.year}"


def _partial_usage_notice(
    month_starts: list[dt.date],
    records: list[FeedUsageRecord],
    *,
    today: dt.date,
) -> str | None:
    open_start = today.replace(day=1)
    if open_start not in month_starts:
        return None
    label = _month_label(open_start)
    has_today = any(
        record.period_start == open_start
        and record.import_timestamp
        and record.import_timestamp.date() == today
        for record in records
    )
    if has_today:
        return f"{label} is still in progress. This refresh is kept for today only."
    return (
        f"{label} is still in progress, so it is left out unless you refresh it today."
    )


def get_usage_report(
    db: Session,
    *,
    month: dt.date,
    farm: str,
    month_to: dt.date | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    current_day = today or dt.date.today()
    discard_expired_partial_usage(db, today=current_day)
    farm_key = normalize_usage_farm(farm)
    month_starts = usage_month_starts(month, month_to or month)
    period_start, _period_end_first = month_bounds(month_starts[0].year, month_starts[0].month)
    _period_start_last, period_end = month_bounds(month_starts[-1].year, month_starts[-1].month)
    farms = list(HERD_FARM_OPTIONS) if farm_key == USAGE_COMBINED_FARM else [farm_key]
    records = list(
        db.scalars(
            select(FeedUsageRecord)
            .where(
                FeedUsageRecord.period_start.in_(month_starts),
                FeedUsageRecord.farm.in_(farms),
            )
            .order_by(FeedUsageRecord.as_fed_kg.desc(), FeedUsageRecord.ingredient_name)
        ).all()
    )
    latest_import = None
    if records:
        latest_import = max(
            (record.import_timestamp for record in records if record.import_timestamp),
            default=None,
        )
    seed_ration_assignments_if_empty(db)
    inclusion = ingredient_inclusion_lookup(db)
    included = [
        record
        for record in records
        if is_usage_ingredient_included(record.ingredient_name, inclusion)
    ]
    if farm_key == USAGE_COMBINED_FARM or len(month_starts) > 1:
        rows = _combined_usage_rows(included)
    else:
        rows = [record.to_dict() for record in included]
    report = build_usage_report(
        rows,
        period_start=period_start,
        period_end=period_end,
        farm=farm_key,
        rations=_usage_ration_names(db, farm_key),
        import_timestamp=latest_import,
    )
    report["partial_notice"] = _partial_usage_notice(
        month_starts, included, today=current_day
    )
    return report


def import_feed_usage(
    db: Session,
    *,
    month: dt.date,
    progress: str | None = None,
    finalize: bool = True,
) -> dict[str, Any]:
    period_start, period_end = month_bounds(month.year, month.month)
    month_key = period_start.strftime("%Y-%m")
    _set_status(
        status="running",
        message=progress or f"Fetching Loaded Mixes for {month_key}…",
        rows_imported=0,
        month=month_key,
        needs_auth=False,
    )
    try:
        rows = fetch_loaded_mix_ingredient_usage(
            db, period_start=period_start, period_end=period_end
        )
        if not rows:
            raise ValueError(
                f"No Loaded Mixes ingredient rows returned from Feedlync for {month_key}."
            )

        import_ts = dt.datetime.now()
        db.execute(
            delete(FeedUsageRecord).where(FeedUsageRecord.period_start == period_start)
        )
        db.flush()

        for row in rows:
            farm = str(row.get("farm") or "").strip().upper()
            if farm not in HERD_FARM_OPTIONS:
                continue
            db.add(
                FeedUsageRecord(
                    period_start=period_start,
                    period_end=period_end,
                    farm=farm,
                    ingredient_name=str(row["ingredient_name"]),
                    as_fed_kg=float(row["as_fed_kg"]),
                    dm_kg=float(row["dm_kg"]),
                    cost=float(row["cost"]),
                    import_timestamp=import_ts,
                )
            )

        db.commit()
        latest_import = import_ts.isoformat()
        result = {
            "rows_imported": len(rows),
            "month": month_key,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "latest_import": latest_import,
        }
        if finalize:
            note = ""
            if usage_import_is_partial(period_end, import_ts.date()):
                note = " This month is still in progress, so these figures are kept for today only."
            _set_status(
                status="complete",
                message=f"Imported {len(rows)} ingredients for {month_key}.{note}",
                latest_import=latest_import,
                rows_imported=len(rows),
                month=month_key,
                needs_auth=False,
            )
        return result
    except FeedlyncAuthError as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), month=month_key, needs_auth=True)
        raise
    except Exception as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), month=month_key, needs_auth=False)
        raise


def import_feed_usage_months(db: Session, *, months: list[dt.date]) -> dict[str, Any]:
    """Refresh each month from Feedlync. Earlier months stay saved if a later one fails."""
    if not months:
        raise ValueError("Choose at least one month to refresh.")
    total_rows = 0
    imported: list[str] = []
    skipped: list[str] = []
    latest_import = None
    for index, month in enumerate(months, start=1):
        month_key = month.strftime("%Y-%m")
        try:
            result = import_feed_usage(
                db,
                month=month,
                progress=f"Fetching Loaded Mixes for {month_key} ({index} of {len(months)})…",
                finalize=False,
            )
        except ValueError as exc:
            if not str(exc).startswith("No Loaded Mixes"):
                raise
            skipped.append(month_key)
            continue
        total_rows += int(result["rows_imported"])
        imported.append(month_key)
        latest_import = result["latest_import"]
    if not imported:
        missing = ", ".join(skipped) or "the selected months"
        message = f"No Loaded Mixes ingredient rows returned from Feedlync for {missing}."
        _set_status(status="error", message=message, month=missing, needs_auth=False)
        raise ValueError(message)
    span = imported[0] if len(imported) == 1 else f"{imported[0]} to {imported[-1]}"
    message = f"Imported {total_rows} ingredients for {span}."
    if skipped:
        message += f" No rows for {', '.join(skipped)}."
    imported_on = dt.date.today()
    if any(
        usage_import_is_partial(month_bounds(int(key[:4]), int(key[5:7]))[1], imported_on)
        for key in imported
    ):
        message += " An unfinished month is kept for today only."
    _set_status(
        status="complete",
        message=message,
        latest_import=latest_import,
        rows_imported=total_rows,
        month=span,
        needs_auth=False,
    )
    return {
        "rows_imported": total_rows,
        "months": imported,
        "skipped": skipped,
        "latest_import": latest_import,
    }


def run_usage_import_in_background(db_factory, month: dt.date | list[dt.date]) -> None:
    months = month if isinstance(month, list) else [month]
    db = db_factory()
    try:
        if len(months) == 1:
            import_feed_usage(db, month=months[0])
        else:
            import_feed_usage_months(db, months=months)
    except Exception:
        pass
    finally:
        db.close()
