"""Milk sales volume forecasts from dairy cow head counts and manual average yield."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, BenchmarkForecastLine
from app.services.benchmarking import available_fiscal_years, fiscal_year_months
from app.services.stock_forecasts import build_stock_forecast_heads_index

MILK_YIELD_METRIC = "milk_yield"
MILK_PRICE_METRIC = "milk_price"


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _subtract_month(value: dt.date) -> dt.date:
    if value.month == 1:
        return dt.date(value.year - 1, 12, 1)
    return dt.date(value.year, value.month - 1, 1)


def _fiscal_year_days(months: list[dt.date]) -> int:
    return sum(calendar.monthrange(month.year, month.month)[1] for month in months)


def _load_milk_yield_index(
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
            BenchmarkForecastLine.metric == MILK_YIELD_METRIC,
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


def _load_milk_price_index(
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
            BenchmarkForecastLine.metric == MILK_PRICE_METRIC,
        )
    ).all()
    for line in lines:
        if line.farm not in farms:
            continue
        if line.forecast_month not in month_set:
            continue
        if line.unit_price is None:
            continue
        index[(line.farm, line.forecast_month)] = float(line.unit_price)
    return index


def _compute_milk_revenue(
    monthly_litres: float | None,
    milk_price_ppl: float | None,
) -> float | None:
    if monthly_litres is None or milk_price_ppl is None:
        return None
    return round(monthly_litres * milk_price_ppl / 100.0)


def _average_cows(
    heads: dict[str, dict[str, dict[str, dict[str, int]]]],
    farm: str,
    month_iso: str,
) -> float:
    dairy = heads.get(farm, {}).get(month_iso, {}).get("Dairy", {})
    opening = int(dairy.get("opening", 0))
    closing = int(dairy.get("closing", 0))
    return (opening + closing) / 2.0


def _sum_available(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present))


def _combined_cells(
    farm_cells: dict[str, dict[str, float | None]],
    farms: list[str],
) -> dict[str, float | None]:
    return {
        "monthly_litres": _sum_available(
            *(farm_cells[farm]["monthly_litres"] for farm in farms)
        ),
        "daily_litres": _sum_available(
            *(farm_cells[farm]["daily_litres"] for farm in farms)
        ),
        "monthly_revenue": _sum_available(
            *(farm_cells[farm]["monthly_revenue"] for farm in farms)
        ),
    }


def _compute_milk_litres(
    *,
    avg_cows: float,
    average_yield: float | None,
    month_days: int,
    fiscal_year_days: int,
) -> tuple[float | None, float | None]:
    if average_yield is None:
        return None, None
    daily_litres = avg_cows * (average_yield / fiscal_year_days)
    monthly_litres = daily_litres * month_days
    return round(monthly_litres), round(daily_litres)


def build_milk_sales_forecasts_report(
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
    fy_days = _fiscal_year_days(months)

    heads = build_stock_forecast_heads_index(
        db,
        farms=farms,
        fiscal_year=fiscal_year,
        today=reference_today,
    )
    yield_index = _load_milk_yield_index(
        db,
        farms=farms,
        month_starts=months,
        fiscal_year=fiscal_year,
    )
    price_index = _load_milk_price_index(
        db,
        farms=farms,
        month_starts=months,
        fiscal_year=fiscal_year,
    )

    missing_yield_months: list[str] = []
    missing_price_months: list[str] = []
    rows: list[dict[str, Any]] = []
    monthly_totals: dict[str, float] = {farm: 0.0 for farm in farms}
    revenue_totals: dict[str, float] = {farm: 0.0 for farm in farms}
    has_monthly: dict[str, bool] = {farm: False for farm in farms}
    has_revenue: dict[str, bool] = {farm: False for farm in farms}

    for month_start in months:
        month_iso = month_start.isoformat()
        month_days = calendar.monthrange(month_start.year, month_start.month)[1]
        farm_cells: dict[str, dict[str, float | None]] = {}

        for farm in farms:
            if (farm, month_start) not in yield_index:
                missing_yield_months.append(f"{farm} {month_start.strftime('%b-%y')}")

            monthly_litres, daily_litres = _compute_milk_litres(
                avg_cows=_average_cows(heads, farm, month_iso),
                average_yield=yield_index.get((farm, month_start)),
                month_days=month_days,
                fiscal_year_days=fy_days,
            )
            milk_price_ppl = price_index.get((farm, month_start))
            if (farm, month_start) not in price_index:
                missing_price_months.append(f"{farm} {month_start.strftime('%b-%y')}")

            farm_cells[farm] = {
                "monthly_litres": monthly_litres,
                "daily_litres": daily_litres,
                "monthly_revenue": _compute_milk_revenue(monthly_litres, milk_price_ppl),
            }
            if monthly_litres is not None:
                monthly_totals[farm] += monthly_litres
                has_monthly[farm] = True
            revenue = farm_cells[farm]["monthly_revenue"]
            if revenue is not None:
                revenue_totals[farm] += revenue
                has_revenue[farm] = True

        farm_cells["Total"] = _combined_cells(farm_cells, farms)

        rows.append({
            "month_start": month_iso,
            "month_label": month_start.strftime("%b-%y"),
            "source": "projected" if month_start >= current_month else "actual",
            "farms": farm_cells,
        })

    totals: dict[str, dict[str, float | None]] = {}
    for farm in farms:
        total_monthly = round(monthly_totals[farm]) if has_monthly[farm] else None
        avg_daily = (
            round(monthly_totals[farm] / fy_days)
            if has_monthly[farm]
            else None
        )
        totals[farm] = {
            "monthly_litres": total_monthly,
            "daily_litres": avg_daily,
            "monthly_revenue": round(revenue_totals[farm]) if has_revenue[farm] else None,
        }

    totals["Total"] = _combined_cells(totals, farms)

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "fiscal_year_days": fy_days,
        "actual_cutoff": last_actual_month.isoformat(),
        "projected_from": current_month.isoformat(),
        "missing_yield_months": sorted(set(missing_yield_months)),
        "missing_price_months": sorted(set(missing_price_months)),
        "rows": rows,
        "totals": totals,
    }
