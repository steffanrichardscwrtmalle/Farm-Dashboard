"""Stock sales and purchase cash forecasts from manual benchmarking forecasts."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, BenchmarkForecastLine
from app.services.benchmarking import (
    BENCHMARK_METRICS,
    available_fiscal_years,
    fiscal_year_months,
    forecast_period_cutoff,
)

SALES_METRICS: tuple[str, ...] = (
    "cull",
    "cow_sale",
    "youngstock_sale",
    "beef_cattle_sale",
    "beef_calf_sale",
)

PURCHASE_METRICS: tuple[str, ...] = (
    "cow_purchase",
    "youngstock_purchase",
)

FORECAST_METRICS: tuple[str, ...] = SALES_METRICS + PURCHASE_METRICS


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _line_value(quantity: float | None, unit_price: float | None) -> float | None:
    if quantity is None or unit_price is None:
        return None
    return round(quantity * unit_price)


def _sum_available(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present))


def _metric_label(metric: str) -> str:
    return str(BENCHMARK_METRICS.get(metric, {}).get("label", metric))


def _load_forecast_index(
    db: Session,
    *,
    fiscal_year: int,
    month_starts: list[dt.date],
) -> dict[tuple[str, dt.date, str], tuple[float | None, float | None]]:
    if not month_starts:
        return {}
    month_set = set(month_starts)
    index: dict[tuple[str, dt.date, str], tuple[float | None, float | None]] = {}
    lines = db.scalars(
        select(BenchmarkForecastLine).where(
            BenchmarkForecastLine.fiscal_year == fiscal_year,
            BenchmarkForecastLine.metric.in_(FORECAST_METRICS),
        )
    ).all()
    for line in lines:
        if line.farm not in HERD_FARM_OPTIONS:
            continue
        if line.forecast_month not in month_set:
            continue
        index[(line.farm, line.forecast_month, line.metric)] = (
            float(line.quantity) if line.quantity is not None else None,
            float(line.unit_price) if line.unit_price is not None else None,
        )
    return index


def _farm_month_values(
    *,
    farm: str,
    month_start: dt.date,
    forecast_index: dict[tuple[str, dt.date, str], tuple[float | None, float | None]],
) -> dict[str, Any]:
    detail: dict[str, float | None] = {}
    for metric in FORECAST_METRICS:
        quantity, unit_price = forecast_index.get((farm, month_start, metric), (None, None))
        detail[metric] = _line_value(quantity, unit_price)

    sales = _sum_available(*(detail[metric] for metric in SALES_METRICS))
    purchases = _sum_available(*(detail[metric] for metric in PURCHASE_METRICS))

    return {
        "purchases": purchases,
        "sales": sales,
        "detail": detail,
    }


def build_stock_sales_purchases_forecasts_report(
    db: Session,
    *,
    fiscal_year: int,
    today: dt.date | None = None,
) -> dict[str, Any]:
    reference_today = today or dt.date.today()
    current_month = _month_start(reference_today)
    months = fiscal_year_months(fiscal_year)
    farms = list(HERD_FARM_OPTIONS)

    forecast_index = _load_forecast_index(
        db,
        fiscal_year=fiscal_year,
        month_starts=months,
    )

    rows: list[dict[str, Any]] = []
    for month_start in months:
        farm_cells = {
            farm: _farm_month_values(
                farm=farm,
                month_start=month_start,
                forecast_index=forecast_index,
            )
            for farm in farms
        }
        rows.append({
            "month_start": month_start.isoformat(),
            "month_label": month_start.strftime("%b-%y"),
            "source": "projected" if month_start >= current_month else "actual",
            "farms": farm_cells,
        })

    cutoff = forecast_period_cutoff(reference_today)

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "actual_cutoff": cutoff["actual_cutoff"],
        "projected_from": cutoff["projected_from"],
        "sales_metrics": [
            {"key": metric, "label": _metric_label(metric)} for metric in SALES_METRICS
        ],
        "purchase_metrics": [
            {"key": metric, "label": _metric_label(metric)} for metric in PURCHASE_METRICS
        ],
        "rows": rows,
    }
