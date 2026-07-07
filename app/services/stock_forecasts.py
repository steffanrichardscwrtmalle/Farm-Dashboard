"""Stock forecasts: actual accruals through last complete month plus projected movements."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    BenchmarkForecastLine,
)
from app.services.benchmarking import available_fiscal_years
from app.services.events_common import (
    SALES_TABLE_REASON_ORDER,
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
    normalize_farms,
)
from app.services.heifers_due import get_heifers_due_report
from app.services.stock_accruals import (
    _fetch_event_count_by_month,
    _last_day_of_month,
    _month_key,
    _month_start,
    build_stock_accruals_report,
)
from app.services.stock_purchases import normalize_stock_group
from app.services.stock_group import VALUATION_CATEGORY_BY_STOCK_GROUP
from app.services.stock_valuations import jv_beef_counts_by_farm

_ZERO_SALES = {reason: 0 for reason in SALES_TABLE_REASON_ORDER}

STOCK_FORECAST_GROUPS: tuple[str, ...] = (
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    STOCK_GROUP_BEEF,
)


def _subtract_month(value: dt.date) -> dt.date:
    if value.month == 1:
        return dt.date(value.year - 1, 12, 1)
    return dt.date(value.year, value.month - 1, 1)


def _forecast_quantity(value: float | None) -> int:
    if value is None:
        return 0
    return int(value)


def _load_forecast_index(
    db: Session,
    *,
    farms: list[str],
    month_starts: list[dt.date],
) -> dict[tuple[str, str, dt.date], int]:
    if not month_starts:
        return {}

    fiscal_years = {_fiscal_year_from_date(month) for month in month_starts}
    month_set = set(month_starts)
    index: dict[tuple[str, str, dt.date], int] = {}

    lines = db.scalars(
        select(BenchmarkForecastLine).where(
            BenchmarkForecastLine.fiscal_year.in_(fiscal_years)
        )
    ).all()
    for line in lines:
        if line.farm not in farms:
            continue
        if line.forecast_month not in month_set:
            continue
        index[(line.metric, line.farm, line.forecast_month)] = _forecast_quantity(
            line.quantity
        )
    return index


def _sum_forecast_qty(
    index: dict[tuple[str, str, dt.date], int],
    metric: str,
    farms: list[str],
    month_start: dt.date,
) -> int:
    return sum(index.get((metric, farm, month_start), 0) for farm in farms)


def _heifers_due_count(
    db: Session,
    *,
    farms: list[str],
    month_start: dt.date,
    heifers_due_index: dict[tuple[str, dt.date], int] | None = None,
) -> int:
    if heifers_due_index is not None:
        return sum(
            heifers_due_index.get((farm, month_start), 0) for farm in farms
        )
    month_label = month_start.strftime("%b-%y")
    report = get_heifers_due_report(
        db,
        farms=farms,
        due_from=month_start,
        due_to=_last_day_of_month(month_start),
    )
    for row in report.get("rows", []):
        if row.get("expected_month") == month_label:
            return sum(int(row.get(farm, 0) or 0) for farm in farms)
    return 0


def _build_heifers_due_index(
    db: Session,
    *,
    farms: list[str],
    month_starts: list[dt.date],
) -> dict[tuple[str, dt.date], int]:
    if not month_starts:
        return {}
    due_from = min(month_starts)
    due_to = _last_day_of_month(max(month_starts))
    report = get_heifers_due_report(
        db,
        farms=farms,
        due_from=due_from,
        due_to=due_to,
    )
    label_to_month = {month.strftime("%b-%y"): month for month in month_starts}
    index: dict[tuple[str, dt.date], int] = {}
    for row in report.get("rows", []):
        month_start = label_to_month.get(row.get("expected_month"))
        if month_start is None:
            continue
        for farm in farms:
            index[(farm, month_start)] = int(row.get(farm, 0) or 0)
    return index


def _already_calved_mtd(
    db: Session,
    *,
    farms: list[str],
    month_start: dt.date,
    through_date: dt.date,
) -> int:
    total = 0
    for farm in farms:
        counts = _fetch_event_count_by_month(
            db,
            farm=farm,
            stock_group=STOCK_GROUP_COWS,
            event_type="FRESH",
            month_from=month_start,
            month_to=through_date,
            lact_filter="fresh_cows",
        )
        total += counts.get(_month_key(month_start), 0)
    return total


def _projected_heifer_calvings(
    db: Session,
    *,
    farms: list[str],
    month_start: dt.date,
    current_month: dt.date,
    today: dt.date,
    heifers_due_index: dict[tuple[str, dt.date], int] | None = None,
) -> int:
    if month_start < current_month:
        return 0
    if month_start > current_month:
        return _heifers_due_count(
            db,
            farms=farms,
            month_start=month_start,
            heifers_due_index=heifers_due_index,
        )
    already = _already_calved_mtd(
        db,
        farms=farms,
        month_start=month_start,
        through_date=today,
    )
    due = _heifers_due_count(
        db,
        farms=farms,
        month_start=month_start,
        heifers_due_index=heifers_due_index,
    )
    return already + due


def _build_projected_row(
    db: Session,
    *,
    farms: list[str],
    stock_group: str,
    month_start: dt.date,
    opening: int,
    current_month: dt.date,
    today: dt.date,
    forecast_index: dict[tuple[str, str, dt.date], int],
    heifers_due_index: dict[tuple[str, dt.date], int] | None = None,
) -> dict[str, Any]:
    sales = dict(_ZERO_SALES)
    deaths = 0
    births = 0
    calvings = 0
    purchases = 0

    heifer_calvings = _projected_heifer_calvings(
        db,
        farms=farms,
        month_start=month_start,
        current_month=current_month,
        today=today,
        heifers_due_index=heifers_due_index,
    )

    if stock_group == STOCK_GROUP_COWS:
        sales["CULL"] = _sum_forecast_qty(forecast_index, "cull", farms, month_start)
        sales["Dairy"] = _sum_forecast_qty(forecast_index, "cow_sale", farms, month_start)
        deaths = _sum_forecast_qty(forecast_index, "cow_death", farms, month_start)
        purchases = _sum_forecast_qty(forecast_index, "cow_purchase", farms, month_start)
        calvings = heifer_calvings
    elif stock_group == STOCK_GROUP_YOUNGSTOCK:
        sales["Dairy"] = _sum_forecast_qty(
            forecast_index, "youngstock_sale", farms, month_start
        )
        deaths = _sum_forecast_qty(forecast_index, "youngstock_death", farms, month_start)
        purchases = _sum_forecast_qty(
            forecast_index, "youngstock_purchase", farms, month_start
        )
        births = _sum_forecast_qty(
            forecast_index, "holstein_calves_born", farms, month_start
        )
        calvings = -heifer_calvings
    elif stock_group == STOCK_GROUP_BEEF:
        sales["Beef"] = _sum_forecast_qty(
            forecast_index, "beef_calf_sale", farms, month_start
        ) + _sum_forecast_qty(forecast_index, "beef_cattle_sale", farms, month_start)
        births = _sum_forecast_qty(forecast_index, "beef_calf_birth", farms, month_start)

    sales_total = sum(sales.values())
    closing = opening - sales_total - deaths + births + calvings + purchases

    return {
        "month_start": month_start.isoformat(),
        "event_month": month_start.strftime("%b-%y"),
        "opening": opening,
        "sales": sales,
        "sales_total": sales_total,
        "deaths": deaths,
        "births": births,
        "calvings": calvings,
        "purchases": purchases,
        "closing": closing,
        "warning": closing < 0 or opening < 0,
        "source": "projected",
    }


def _closing_for_month(
    rows: list[dict[str, Any]],
    month_start: dt.date,
) -> int | None:
    target = month_start.isoformat()
    for row in rows:
        if row["month_start"] == target:
            return int(row["closing"])
    return None


def _closing_through_month(
    rows: list[dict[str, Any]],
    month_start: dt.date,
) -> int | None:
    target = month_start.isoformat()
    closing: int | None = None
    for row in rows:
        if row["month_start"] <= target:
            closing = row["closing"]
    return closing


def _fiscal_year_month_range(fiscal_year: int) -> tuple[dt.date, dt.date]:
    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    return _month_start(fy_start), _month_start(fy_end)


@dataclass
class _ForecastSharedContext:
    current_month: dt.date
    last_actual_month: dt.date
    fy_start_month: dt.date
    fy_end_month: dt.date
    projected_month_starts: list[dt.date]
    forecast_index: dict[tuple[str, str, dt.date], int]
    heifers_due_index: dict[tuple[str, dt.date], int]
    jv_beef_by_farm: dict[str, int]
    today: dt.date


def _build_forecast_shared_context(
    db: Session,
    *,
    farms: list[str],
    fiscal_year: int,
    today: dt.date,
) -> _ForecastSharedContext:
    current_month = _month_start(today)
    last_actual_month = _subtract_month(current_month)
    fy_start_month, fy_end_month = _fiscal_year_month_range(fiscal_year)
    projected_from = max(current_month, fy_start_month)
    projected_month_starts = [
        month
        for month in _iter_month_starts(projected_from, fy_end_month)
        if month >= current_month
    ]
    return _ForecastSharedContext(
        current_month=current_month,
        last_actual_month=last_actual_month,
        fy_start_month=fy_start_month,
        fy_end_month=fy_end_month,
        projected_month_starts=projected_month_starts,
        forecast_index=_load_forecast_index(
            db, farms=farms, month_starts=projected_month_starts
        ),
        heifers_due_index=_build_heifers_due_index(
            db, farms=farms, month_starts=projected_month_starts
        ),
        jv_beef_by_farm=jv_beef_counts_by_farm(
            db,
            farms=farms,
            close_date=_last_day_of_month(last_actual_month),
        ),
        today=today,
    )


def _build_stock_forecast_rows(
    db: Session,
    *,
    farms: list[str],
    stock_group: str,
    fiscal_year: int,
    shared: _ForecastSharedContext,
) -> list[dict[str, Any]]:
    group = normalize_stock_group(stock_group)
    seed_report = build_stock_accruals_report(
        db,
        farms=farms,
        stock_group=group,
        month_to=_last_day_of_month(shared.last_actual_month),
    )

    actual_rows: list[dict[str, Any]] = []
    for row in seed_report.get("rows", []):
        row_month = dt.date.fromisoformat(row["month_start"])
        if row_month >= shared.current_month:
            continue
        if row_month < shared.fy_start_month or row_month > shared.fy_end_month:
            continue
        actual_rows.append({**row, "source": "actual"})

    opening = _closing_through_month(
        [
            row
            for row in seed_report.get("rows", [])
            if dt.date.fromisoformat(row["month_start"]) < shared.current_month
        ],
        shared.last_actual_month,
    )
    if opening is None and actual_rows:
        opening = actual_rows[-1]["closing"]

    jv_beef_total = 0
    if group == STOCK_GROUP_BEEF:
        jv_beef_total = sum(shared.jv_beef_by_farm.get(farm, 0) for farm in farms)

    projected_rows: list[dict[str, Any]] = []
    rolling_opening = opening if opening is not None else 0
    if (
        shared.projected_month_starts
        and shared.projected_month_starts[0] == shared.fy_start_month
    ):
        prior_fy_month = _subtract_month(shared.fy_start_month)
        if prior_fy_month <= shared.last_actual_month:
            prior_closing = _closing_for_month(
                seed_report.get("rows", []),
                prior_fy_month,
            )
            if prior_closing is not None:
                rolling_opening = prior_closing
    if group == STOCK_GROUP_BEEF:
        rolling_opening = max(0, rolling_opening - jv_beef_total)

    for month_start in shared.projected_month_starts:
        row = _build_projected_row(
            db,
            farms=farms,
            stock_group=group,
            month_start=month_start,
            opening=rolling_opening,
            current_month=shared.current_month,
            today=shared.today,
            forecast_index=shared.forecast_index,
            heifers_due_index=shared.heifers_due_index,
        )
        if group == STOCK_GROUP_BEEF:
            row["opening"] = max(0, row["opening"] - jv_beef_total)
            row["closing"] = max(0, row["closing"] - jv_beef_total)
        projected_rows.append(row)
        rolling_opening = row["closing"]

    combined = actual_rows + projected_rows
    combined.sort(key=lambda row: row["month_start"])
    return combined


def build_stock_forecast_heads_index(
    db: Session,
    *,
    farms: list[str] | None = None,
    fiscal_year: int,
    today: dt.date | None = None,
) -> dict[str, dict[str, dict[str, dict[str, int]]]]:
    """Per-farm projected head counts for all valuation categories in one pass."""
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {}

    reference_today = today or dt.date.today()
    shared = _build_forecast_shared_context(
        db,
        farms=selected_farms,
        fiscal_year=fiscal_year,
        today=reference_today,
    )

    heads: dict[str, dict[str, dict[str, dict[str, int]]]] = {
        farm: {} for farm in selected_farms
    }
    for stock_group in STOCK_FORECAST_GROUPS:
        category = VALUATION_CATEGORY_BY_STOCK_GROUP[stock_group]
        for farm in selected_farms:
            rows = _build_stock_forecast_rows(
                db,
                farms=[farm],
                stock_group=stock_group,
                fiscal_year=fiscal_year,
                shared=shared,
            )
            for row in rows:
                month_key = row["month_start"]
                heads[farm].setdefault(month_key, {})[category] = {
                    "opening": int(row["opening"]),
                    "closing": int(row["closing"]),
                }
    return heads


def build_stock_forecasts_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_group: str | None = None,
    fiscal_year: int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    group = normalize_stock_group(stock_group)
    reference_today = today or dt.date.today()
    current_month = _month_start(reference_today)
    last_actual_month = _subtract_month(current_month)

    year_options = available_fiscal_years()
    year = fiscal_year if fiscal_year is not None else year_options[0]
    if year not in year_options:
        year = year_options[0]

    fy_start_month, fy_end_month = _fiscal_year_month_range(year)

    empty: dict[str, Any] = {
        "rows": [],
        "sales_reasons": list(SALES_TABLE_REASON_ORDER),
        "stock_group": group,
        "date_bounds": {
            "min": fy_start_month.isoformat(),
            "max": _last_day_of_month(fy_end_month).isoformat(),
        },
        "fiscal_year_options": year_options,
        "selected_fiscal_year": year,
        "actual_cutoff": last_actual_month.isoformat(),
        "projected_from": current_month.isoformat(),
    }

    if not selected_farms:
        return empty

    shared = _build_forecast_shared_context(
        db,
        farms=selected_farms,
        fiscal_year=year,
        today=reference_today,
    )
    combined = _build_stock_forecast_rows(
        db,
        farms=selected_farms,
        stock_group=group,
        fiscal_year=year,
        shared=shared,
    )

    return {
        "rows": combined,
        "sales_reasons": list(SALES_TABLE_REASON_ORDER),
        "stock_group": group,
        "date_bounds": {
            "min": fy_start_month.isoformat(),
            "max": _last_day_of_month(fy_end_month).isoformat(),
        },
        "fiscal_year_options": year_options,
        "selected_fiscal_year": year,
        "actual_cutoff": shared.last_actual_month.isoformat(),
        "projected_from": shared.current_month.isoformat(),
    }
