"""Shared month pivot for cow event reports."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, CowEvent

EVENT_PAGE_TYPES: dict[str, tuple[str, ...]] = {
    "calvings": ("FRESH",),
    "sales": ("SOLD",),
    "deaths": ("DIED",),
    "breedings": ("BRED",),
    "disease": ("ILL", "SCOURS", "LAME", "MAST", "METR", "RESP", "INJURY", "ABORT", "DA"),
}

LACTATION_GROUPS: tuple[str, ...] = ("1", "2", "3+")
PARITY_GROUPS: tuple[str, ...] = ("primiparous", "multiparous")
PAGES_WITH_PARITY_FILTER: frozenset[str] = frozenset({"sales", "deaths", "disease", "breedings"})
SALES_REASON_ORDER: tuple[str, ...] = ("OFS", "TB", "Beef", "CULL")


def normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def normalize_lact_groups(lact_groups: list[str] | None) -> list[str] | None:
    if not lact_groups:
        return None
    selected = [group for group in lact_groups if group in LACTATION_GROUPS]
    return selected or None


def _apply_lact_groups(query, lact_groups: list[str] | None):
    if not lact_groups:
        return query
    conditions = []
    if "1" in lact_groups:
        conditions.append(CowEvent.lact == 1)
    if "2" in lact_groups:
        conditions.append(CowEvent.lact == 2)
    if "3+" in lact_groups:
        conditions.append(CowEvent.lact >= 3)
    if not conditions:
        return query
    return query.where(or_(*conditions))


def normalize_parity_groups(parity_groups: list[str] | None) -> list[str] | None:
    if not parity_groups:
        return None
    selected = [group for group in parity_groups if group in PARITY_GROUPS]
    return selected or None


def _apply_parity_groups(query, parity_groups: list[str] | None):
    if not parity_groups:
        return query
    conditions = []
    if "primiparous" in parity_groups:
        conditions.append(CowEvent.lact == 0)
    if "multiparous" in parity_groups:
        conditions.append(CowEvent.lact > 0)
    if not conditions:
        return query
    return query.where(or_(*conditions))


def _fiscal_year_from_date(value: dt.date) -> int:
    return value.year + 1 if value.month >= 4 else value.year


def _sort_key_from_date(value: dt.date) -> int:
    month = value.month
    fiscal_year = _fiscal_year_from_date(value)
    month_adjusted = month - 3 if month >= 4 else month + 9
    return fiscal_year * 100 + month_adjusted


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _iter_month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    current = _month_start(start)
    end_month = _month_start(end)
    months: list[dt.date] = []
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def _month_count_inclusive(event_from: dt.date, event_to: dt.date) -> int:
    return len(_iter_month_starts(event_from, event_to))


def _build_range_summary(grand_cm: int, grand_gad: int, month_count: int) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / month_count, 1) if month_count else 0.0

    grand_total = grand_cm + grand_gad
    return {
        "total": grand_total,
        "month_count": month_count,
        "average_per_month": avg(grand_total),
        "CM": {"total": grand_cm, "average_per_month": avg(grand_cm)},
        "GAD": {"total": grand_gad, "average_per_month": avg(grand_gad)},
    }


def _empty_range_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "month_count": 0,
        "average_per_month": 0,
        "CM": {"total": 0, "average_per_month": 0},
        "GAD": {"total": 0, "average_per_month": 0},
    }



def _get_fiscal_year_options(
    db: Session,
    event_types: tuple[str, ...],
    selected_farms: list[str],
) -> list[int]:
    rows = db.execute(
        select(CowEvent.fiscal_year)
        .where(CowEvent.event.in_(list(event_types)))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.fiscal_year.isnot(None))
        .distinct()
        .order_by(CowEvent.fiscal_year.desc())
    ).all()
    return [int(row[0]) for row in rows if row[0] is not None]


def _apply_fiscal_year(query, fiscal_year: int | None):
    if fiscal_year is None:
        return query
    return query.where(CowEvent.fiscal_year == fiscal_year)


def _sales_reason_expression():
    return case(
        (CowEvent.remark == "OFS", literal("OFS")),
        (CowEvent.remark == "CAR11", literal("TB")),
        (CowEvent.remark == "CAR16", literal("Beef")),
        else_=literal("CULL"),
    )


def _build_sales_reason_rows(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
) -> list[dict[str, Any]]:
    reason_expr = _sales_reason_expression()
    counts_query = (
        select(
            CowEvent.month_label,
            reason_expr.label("reason"),
            CowEvent.farm,
            func.count(),
        )
        .where(CowEvent.event == "SOLD")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    counts_query = _apply_parity_groups(counts_query, selected_parity_groups)
    counts_query = _apply_fiscal_year(counts_query, fiscal_year)
    counts = db.execute(
        counts_query.group_by(CowEvent.month_label, reason_expr, CowEvent.farm).order_by(
            func.min(CowEvent.sort_key), reason_expr
        )
    ).all()

    pivot: dict[str, dict[str, dict[str, int]]] = {}
    for month_label, reason, farm, count in counts:
        if not month_label or not reason:
            continue
        month_key = str(month_label)
        reason_key = str(reason)
        pivot.setdefault(month_key, {})
        pivot[month_key].setdefault(reason_key, {"CM": 0, "GAD": 0})
        if farm in pivot[month_key][reason_key]:
            pivot[month_key][reason_key][farm] = int(count)

    reason_rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(effective_from, effective_to):
        event_month = month_start.strftime("%b-%y")
        month_reasons = pivot.get(event_month, {})
        for reason in SALES_REASON_ORDER:
            counts_by_farm = month_reasons.get(reason, {"CM": 0, "GAD": 0})
            cm = counts_by_farm.get("CM", 0)
            gad = counts_by_farm.get("GAD", 0)
            total = cm + gad
            if total == 0:
                continue
            reason_rows.append(
                {
                    "event_month": event_month,
                    "reason": reason,
                    "CM": cm,
                    "GAD": gad,
                    "total": total,
                }
            )
    return reason_rows


def _fiscal_year_calendar_bounds(fiscal_year: int) -> tuple[dt.date, dt.date]:
    """UK fiscal year: Apr (FY-1) through Mar (FY)."""
    return dt.date(fiscal_year - 1, 4, 1), dt.date(fiscal_year, 3, 31)


def _clamp_date(value: dt.date, min_date: dt.date, max_date: dt.date) -> dt.date:
    return max(min_date, min(value, max_date))


def _get_date_bounds(
    db: Session,
    event_types: tuple[str, ...],
    selected_farms: list[str],
) -> tuple[dt.date | None, dt.date | None]:
    row = db.execute(
        select(
            func.min(CowEvent.event_date),
            func.max(CowEvent.event_date),
        )
        .where(CowEvent.event.in_(list(event_types)))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
    ).one()
    min_date = row[0]
    max_date = row[1]
    if min_date is None or max_date is None:
        return None, None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return min_date, max_date


def _zero_fill_rows(
    pivot: dict[str, dict[str, int]],
    event_from: dt.date,
    event_to: dt.date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(event_from, event_to):
        sort_key = _sort_key_from_date(month_start)
        event_month = month_start.strftime("%b-%y")
        counts = pivot.get(event_month, {"CM": 0, "GAD": 0})
        cm = counts.get("CM", 0)
        gad = counts.get("GAD", 0)
        rows.append(
            {
                "event_month": event_month,
                "sort_key": sort_key,
                "CM": cm,
                "GAD": gad,
                "total": cm + gad,
            }
        )
    return rows


def build_events_report(
    db: Session,
    *,
    event_types: tuple[str, ...],
    farms: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    lact_groups: list[str] | None = None,
    parity_groups: list[str] | None = None,
    fiscal_year: int | None = None,
    include_sales_reason_breakdown: bool = False,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_lact_groups = normalize_lact_groups(lact_groups)
    selected_parity_groups = normalize_parity_groups(parity_groups)
    latest_import = db.scalar(select(func.max(CowEvent.import_timestamp)))

    empty_result: dict[str, Any] = {
        "rows": [],
        "grand_total": {"CM": 0, "GAD": 0, "total": 0},
        "range_summary": _empty_range_summary(),
        "fiscal_year_options": [],
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_sales_reason_breakdown:
        empty_result["reason_rows"] = []

    if not selected_farms:
        return empty_result

    fiscal_year_options = _get_fiscal_year_options(db, event_types, selected_farms)
    empty_result["fiscal_year_options"] = fiscal_year_options

    bounds_min, bounds_max = _get_date_bounds(db, event_types, selected_farms)
    if bounds_min is None or bounds_max is None:
        empty_result["date_bounds"] = None
        return empty_result

    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    date_bounds = {
        "min": slider_min.isoformat(),
        "max": slider_max.isoformat(),
    }

    effective_from = event_from if event_from is not None else slider_min
    effective_to = event_to if event_to is not None else slider_max
    effective_from = _clamp_date(effective_from, slider_min, slider_max)
    effective_to = _clamp_date(effective_to, slider_min, slider_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    counts_query = (
        select(
            CowEvent.month_label,
            CowEvent.farm,
            func.count(),
        )
        .where(CowEvent.event.in_(list(event_types)))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    counts_query = _apply_lact_groups(counts_query, selected_lact_groups)
    counts_query = _apply_parity_groups(counts_query, selected_parity_groups)
    counts_query = _apply_fiscal_year(counts_query, fiscal_year)
    counts = db.execute(
        counts_query.group_by(CowEvent.month_label, CowEvent.farm).order_by(
            func.min(CowEvent.sort_key)
        )
    ).all()

    pivot: dict[str, dict[str, int]] = {}
    for month_label, farm, count in counts:
        if not month_label:
            continue
        key = str(month_label)
        pivot.setdefault(key, {"CM": 0, "GAD": 0})
        if farm in pivot[key]:
            pivot[key][farm] = int(count)

    rows = _zero_fill_rows(pivot, effective_from, effective_to)

    grand_cm = sum(row["CM"] for row in rows)
    grand_gad = sum(row["GAD"] for row in rows)
    grand_total = grand_cm + grand_gad
    month_count = _month_count_inclusive(effective_from, effective_to)

    result: dict[str, Any] = {
        "rows": rows,
        "grand_total": {
            "CM": grand_cm,
            "GAD": grand_gad,
            "total": grand_total,
        },
        "date_bounds": date_bounds,
        "range_summary": _build_range_summary(grand_cm, grand_gad, month_count),
        "fiscal_year_options": fiscal_year_options,
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_sales_reason_breakdown:
        result["reason_rows"] = _build_sales_reason_rows(
            db,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            selected_parity_groups=selected_parity_groups,
            fiscal_year=fiscal_year,
        )
    return result


def build_events_page_report(
    db: Session,
    *,
    page_slug: str,
    farms: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    lact_groups: list[str] | None = None,
    parity_groups: list[str] | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    event_types = EVENT_PAGE_TYPES.get(page_slug)
    if not event_types:
        raise ValueError(f"Unknown events page: {page_slug}")
    return build_events_report(
        db,
        event_types=event_types,
        farms=farms,
        event_from=event_from,
        event_to=event_to,
        lact_groups=lact_groups if page_slug == "calvings" else None,
        parity_groups=parity_groups if page_slug in PAGES_WITH_PARITY_FILTER else None,
        fiscal_year=fiscal_year,
        include_sales_reason_breakdown=page_slug == "sales",
    )
