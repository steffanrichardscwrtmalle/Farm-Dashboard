"""Manual monthly forecast/budget figures for the Benchmarking section."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, BenchmarkForecastLine
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
)

BENCHMARK_METRICS: dict[str, dict[str, Any]] = {
    "cull": {
        "label": "Cull Cows",
        "category": "cow",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "cow_sale": {
        "label": "Cow Sales (Dairy)",
        "category": "cow",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "cow_purchase": {
        "label": "Cow Purchases",
        "category": "cow",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "youngstock_purchase": {
        "label": "Youngstock Purchases",
        "category": "youngstock",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "youngstock_sale": {
        "label": "Youngstock Sales",
        "category": "youngstock",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "beef_calf_sale": {
        "label": "Beef Calf Sales",
        "category": "beef",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "holstein_calves_born": {
        "label": "Holstein Calves Born",
        "category": "cow",
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "Head count",
        "price_label": None,
    },
    "beef_cattle_sale": {
        "label": "Beef Cattle Sales",
        "category": "beef",
        "has_quantity": True,
        "has_price": True,
        "quantity_label": "Head count",
        "price_label": "£/head",
    },
    "cow_death": {
        "label": "Cow Deaths",
        "category": "cow",
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "Head count",
        "price_label": None,
    },
    "youngstock_death": {
        "label": "Youngstock Deaths",
        "category": "youngstock",
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "Head count",
        "price_label": None,
    },
    "milk_price": {
        "label": "Milk Price",
        "category": "cow",
        "has_quantity": False,
        "has_price": True,
        "quantity_label": None,
        "price_label": "ppl",
    },
    "milk_yield": {
        "label": "Average Yield",
        "category": "cow",
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "litres/cow",
        "price_label": None,
    },
    "dry_cows_pct": {
        "label": "Dry Cows (%)",
        "category": "cow",
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "% dry",
        "price_label": None,
    },
}

BENCHMARK_METRIC_KEYS: tuple[str, ...] = tuple(BENCHMARK_METRICS.keys())

BENCHMARK_CATEGORY_ORDER: tuple[str, ...] = ("cow", "youngstock", "beef")


def available_fiscal_years() -> list[int]:
    """Current UK fiscal year and the next."""
    current = _fiscal_year_from_date(dt.date.today())
    return [current, current + 1]


def fiscal_year_months(fiscal_year: int) -> list[dt.date]:
    start, end = _fiscal_year_calendar_bounds(fiscal_year)
    return _iter_month_starts(start, end)


def list_metric_definitions() -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = {
        cat: [] for cat in BENCHMARK_CATEGORY_ORDER
    }
    for key, meta in BENCHMARK_METRICS.items():
        by_category[meta["category"]].append({"id": key, **meta})
    result: list[dict[str, Any]] = []
    for cat in BENCHMARK_CATEGORY_ORDER:
        result.extend(by_category[cat])
    return result


def _empty_farm_cells() -> dict[str, dict[str, float | None]]:
    return {farm: {"quantity": None, "unit_price": None} for farm in HERD_FARM_OPTIONS}


def _line_to_cells(line: BenchmarkForecastLine) -> dict[str, float | None]:
    return {"quantity": line.quantity, "unit_price": line.unit_price}


def list_forecasts(db: Session, *, fiscal_year: int) -> dict[str, Any]:
    months = fiscal_year_months(fiscal_year)
    month_set = set(months)

    stored = db.scalars(
        select(BenchmarkForecastLine).where(
            BenchmarkForecastLine.fiscal_year == fiscal_year
        )
    ).all()

    by_metric_month: dict[str, dict[dt.date, dict[str, dict[str, float | None]]]] = {
        metric: {} for metric in BENCHMARK_METRIC_KEYS
    }
    for line in stored:
        if line.forecast_month not in month_set:
            continue
        if line.metric not in by_metric_month:
            continue
        if line.farm not in HERD_FARM_OPTIONS:
            continue
        month_bucket = by_metric_month[line.metric].setdefault(
            line.forecast_month, _empty_farm_cells()
        )
        month_bucket[line.farm] = _line_to_cells(line)

    metrics_payload: dict[str, Any] = {}
    for metric in BENCHMARK_METRIC_KEYS:
        rows: list[dict[str, Any]] = []
        for month_start in months:
            farms = by_metric_month[metric].get(month_start, _empty_farm_cells())
            rows.append(
                {
                    "forecast_month": month_start.isoformat(),
                    "month_label": month_start.strftime("%b-%y"),
                    "CM": dict(farms.get("CM", {"quantity": None, "unit_price": None})),
                    "GAD": dict(farms.get("GAD", {"quantity": None, "unit_price": None})),
                }
            )
        metrics_payload[metric] = {"rows": rows}

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "months": [m.isoformat() for m in months],
        "metrics": metrics_payload,
    }


def _parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def save_forecasts(
    db: Session,
    *,
    fiscal_year: int,
    metric: str,
    rows: list[dict[str, Any]],
    user_id: int | None,
) -> dict[str, Any]:
    if metric not in BENCHMARK_METRICS:
        raise ValueError(f"Unknown metric: {metric}")

    valid_months = {m.isoformat() for m in fiscal_year_months(fiscal_year)}
    updated = 0
    deleted = 0

    for row in rows:
        forecast_month_raw = row.get("forecast_month")
        farm = row.get("farm")
        if not forecast_month_raw or farm not in HERD_FARM_OPTIONS:
            continue
        if isinstance(forecast_month_raw, dt.date):
            forecast_month = forecast_month_raw
        else:
            forecast_month = dt.date.fromisoformat(str(forecast_month_raw))
        if forecast_month.isoformat() not in valid_months:
            continue

        quantity = _parse_optional_float(row.get("quantity"))
        unit_price = _parse_optional_float(row.get("unit_price"))

        existing = db.scalar(
            select(BenchmarkForecastLine).where(
                BenchmarkForecastLine.fiscal_year == fiscal_year,
                BenchmarkForecastLine.forecast_month == forecast_month,
                BenchmarkForecastLine.metric == metric,
                BenchmarkForecastLine.farm == farm,
            )
        )

        if quantity is None and unit_price is None:
            if existing is not None:
                db.delete(existing)
                deleted += 1
            continue

        if existing is None:
            db.add(
                BenchmarkForecastLine(
                    fiscal_year=fiscal_year,
                    forecast_month=forecast_month,
                    metric=metric,
                    farm=farm,
                    quantity=quantity,
                    unit_price=unit_price,
                    updated_by_user_id=user_id,
                )
            )
            updated += 1
        else:
            existing.quantity = quantity
            existing.unit_price = unit_price
            existing.updated_by_user_id = user_id
            updated += 1

    db.commit()
    return {"metric": metric, "fiscal_year": fiscal_year, "updated": updated, "deleted": deleted}
