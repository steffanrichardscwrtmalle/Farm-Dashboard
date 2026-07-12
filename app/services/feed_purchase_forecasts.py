"""Feed purchase forecasts from stock head counts, dry %, and ration category costs."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, BenchmarkForecastLine
from app.services.benchmarking import available_fiscal_years, fiscal_year_months
from app.services.benchmarking_farm_rations import (
    FEED_INGREDIENT_CATEGORIES,
    ration_costs_by_suffix,
)
from app.services.stock_forecasts import build_stock_forecast_heads_index

DRY_COWS_METRIC = "dry_cows_pct"

LINE_ITEM_KEYS = (
    "milkers",
    "far_off",
    "close_up",
    "calf",
    "pre_bullers",
    "bullers",
    "pregnant_heifers",
)


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _subtract_month(value: dt.date) -> dt.date:
    if value.month == 1:
        return dt.date(value.year - 1, 12, 1)
    return dt.date(value.year, value.month - 1, 1)


def _load_dry_pct_index(
    db: Session,
    *,
    farms: list[str],
    month_starts: list[dt.date],
    fiscal_year: int,
) -> dict[tuple[str, dt.date], float]:
    if not month_starts:
        return {}
    month_set = set(month_starts)
    index: dict[tuple[str, dt.date], float] = {}
    lines = db.scalars(
        select(BenchmarkForecastLine).where(
            BenchmarkForecastLine.fiscal_year == fiscal_year,
            BenchmarkForecastLine.metric == DRY_COWS_METRIC,
        )
    ).all()
    for line in lines:
        if line.farm not in farms:
            continue
        if line.forecast_month not in month_set:
            continue
        if line.quantity is None:
            continue
        index[(line.farm, line.forecast_month)] = float(line.quantity)
    return index


def _head_count(
    heads: dict[str, dict[str, dict[str, dict[str, int]]]],
    farm: str,
    month_iso: str,
    category: str,
    field: str,
) -> int:
    return int(
        heads.get(farm, {})
        .get(month_iso, {})
        .get(category, {})
        .get(field, 0)
    )


def _rate(
    ration_costs: dict[str, dict[str, dict[str, float | None]]],
    suffix: str,
    month_iso: str,
    category: str,
) -> float | None:
    return ration_costs.get(suffix, {}).get(month_iso, {}).get(category)


def _line_cost(
    *,
    average_heads: float,
    factor: float,
    rate: float | None,
    days: int,
) -> float | None:
    if rate is None:
        return None
    return average_heads * factor * rate * days


def _sum_available(*values: float | None) -> float | None:
    """Sum non-null values; null only when every component is missing."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present))


def _compute_month_category(
    *,
    farm: str,
    month_start: dt.date,
    category: str,
    heads: dict[str, dict[str, dict[str, dict[str, int]]]],
    ration_costs: dict[str, dict[str, dict[str, float | None]]],
    dry_pct: float,
) -> dict[str, Any]:
    month_iso = month_start.isoformat()
    days = calendar.monthrange(month_start.year, month_start.month)[1]
    dry = dry_pct / 100.0

    open_dairy = _head_count(heads, farm, month_iso, "Dairy", "opening")
    close_dairy = _head_count(heads, farm, month_iso, "Dairy", "closing")
    open_ys = _head_count(heads, farm, month_iso, "Youngstock", "opening")
    close_ys = _head_count(heads, farm, month_iso, "Youngstock", "closing")
    open_beef = _head_count(heads, farm, month_iso, "Beef", "opening")
    close_beef = _head_count(heads, farm, month_iso, "Beef", "closing")

    avg_cows = (open_dairy + close_dairy) / 2.0
    avg_young_beef = (open_ys + open_beef + close_ys + close_beef) / 2.0

    rate_milkers = _rate(ration_costs, "milkers", month_iso, category)
    rate_far_off = _rate(ration_costs, "far_off", month_iso, category)
    rate_close_up = _rate(ration_costs, "close_up", month_iso, category)
    rate_bullers = _rate(ration_costs, "bullers", month_iso, category)

    detail = {
        "milkers": _line_cost(
            average_heads=avg_cows,
            factor=1.0 - dry,
            rate=rate_milkers,
            days=days,
        ),
        "far_off": _line_cost(
            average_heads=avg_cows,
            factor=dry * 0.5,
            rate=rate_far_off,
            days=days,
        ),
        "close_up": _line_cost(
            average_heads=avg_cows,
            factor=dry * 0.5,
            rate=rate_close_up,
            days=days,
        ),
        "calf": _line_cost(
            average_heads=avg_young_beef,
            factor=0.125,
            rate=rate_milkers,
            days=days,
        ),
        "pre_bullers": _line_cost(
            average_heads=avg_young_beef,
            factor=0.3333 * 0.18,
            rate=rate_milkers,
            days=days,
        ),
        "bullers": _line_cost(
            average_heads=avg_young_beef,
            factor=0.15,
            rate=rate_bullers,
            days=days,
        ),
        "pregnant_heifers": _line_cost(
            average_heads=avg_young_beef,
            factor=0.15 * 0.75,
            rate=rate_far_off,
            days=days,
        ),
    }

    dairy = _sum_available(detail["milkers"], detail["far_off"], detail["close_up"])
    youngstock = _sum_available(
        detail["calf"],
        detail["pre_bullers"],
        detail["bullers"],
        detail["pregnant_heifers"],
    )

    return {
        "dairy": dairy,
        "youngstock": youngstock,
        "detail": {key: round(value) if value is not None else None for key, value in detail.items()},
    }


def _build_farm_tables(
    *,
    farm: str,
    fiscal_year: int,
    months: list[dt.date],
    heads: dict[str, dict[str, dict[str, dict[str, int]]]],
    ration_costs: dict[str, dict[str, dict[str, float | None]]],
    dry_index: dict[tuple[str, dt.date], float],
    current_month: dt.date,
    missing_dry_months: list[str],
) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for category in FEED_INGREDIENT_CATEGORIES:
        rows: list[dict[str, Any]] = []
        for month_start in months:
            month_iso = month_start.isoformat()
            dry_pct = dry_index.get((farm, month_start), 0.0)
            if (farm, month_start) not in dry_index:
                missing_dry_months.append(f"{farm} {month_start.strftime('%b-%y')}")

            computed = _compute_month_category(
                farm=farm,
                month_start=month_start,
                category=category,
                heads=heads,
                ration_costs=ration_costs,
                dry_pct=dry_pct,
            )
            row: dict[str, Any] = {
                "month_start": month_iso,
                "month_label": month_start.strftime("%b-%y"),
                "source": "projected" if month_start >= current_month else "actual",
                "dairy": computed["dairy"],
                "youngstock": computed["youngstock"],
                "detail": computed["detail"],
            }
            if category in ("forage", "straw"):
                row["total"] = _sum_available(computed["dairy"], computed["youngstock"])
            rows.append(row)
        tables[category] = {"rows": rows}
    return tables


def build_feed_purchase_forecasts_report(
    db: Session,
    *,
    fiscal_year: int,
    today: dt.date | None = None,
) -> dict[str, Any]:
    reference_today = today or dt.date.today()
    current_month = _month_start(reference_today)
    last_actual_month = _subtract_month(current_month)

    months = fiscal_year_months(fiscal_year)
    farms = list(HERD_FARM_OPTIONS)

    heads = build_stock_forecast_heads_index(
        db,
        farms=farms,
        fiscal_year=fiscal_year,
        today=reference_today,
    )
    dry_index = _load_dry_pct_index(
        db,
        farms=farms,
        month_starts=months,
        fiscal_year=fiscal_year,
    )

    missing_dry_months: list[str] = []
    farm_payloads: dict[str, Any] = {}
    for farm in farms:
        ration_costs = ration_costs_by_suffix(db, farm=farm, fiscal_year=fiscal_year)
        farm_payloads[farm] = {
            "tables": _build_farm_tables(
                farm=farm,
                fiscal_year=fiscal_year,
                months=months,
                heads=heads,
                ration_costs=ration_costs,
                dry_index=dry_index,
                current_month=current_month,
                missing_dry_months=missing_dry_months,
            ),
        }

    unique_missing = sorted(set(missing_dry_months))

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "actual_cutoff": last_actual_month.isoformat(),
        "projected_from": current_month.isoformat(),
        "missing_dry_pct_months": unique_missing,
        "farms": farm_payloads,
    }
