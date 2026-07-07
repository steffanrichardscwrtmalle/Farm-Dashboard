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
        "has_births": True,
        "births_metric": "beef_calf_birth",
        "births_label": "Births",
        "quantity_label": "Sales",
        "price_label": "£/head",
    },
    "beef_calf_birth": {
        "label": "Beef Calf Births",
        "category": "beef",
        "hide_tab": True,
        "has_quantity": True,
        "has_price": False,
        "quantity_label": "Births",
        "price_label": None,
    },
    "holstein_calves_born": {
        "label": "Youngstock Born",
        "category": "youngstock",
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


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _subtract_month(value: dt.date) -> dt.date:
    if value.month == 1:
        return dt.date(value.year - 1, 12, 1)
    return dt.date(value.year, value.month - 1, 1)


def forecast_period_cutoff(today: dt.date | None = None) -> dict[str, str]:
    reference = today or dt.date.today()
    current_month = _month_start(reference)
    last_actual_month = _subtract_month(current_month)
    return {
        "projected_from": current_month.isoformat(),
        "actual_cutoff": last_actual_month.isoformat(),
    }


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
        if meta.get("hide_tab"):
            continue
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
            farm_cells = _farm_cells_for_metric_row(metric, month_start, by_metric_month)
            rows.append(
                {
                    "forecast_month": month_start.isoformat(),
                    "month_label": month_start.strftime("%b-%y"),
                    "CM": dict(farm_cells.get("CM", {"quantity": None, "unit_price": None})),
                    "GAD": dict(farm_cells.get("GAD", {"quantity": None, "unit_price": None})),
                }
            )
        metrics_payload[metric] = {"rows": rows}

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "months": [m.isoformat() for m in months],
        "metrics": metrics_payload,
        **forecast_period_cutoff(),
    }


def _parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upsert_forecast_line(
    db: Session,
    *,
    fiscal_year: int,
    forecast_month: dt.date,
    metric: str,
    farm: str,
    quantity: float | None,
    unit_price: float | None,
    user_id: int | None,
) -> tuple[int, int]:
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
            return 0, 1
        return 0, 0

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
        return 1, 0

    existing.quantity = quantity
    existing.unit_price = unit_price
    existing.updated_by_user_id = user_id
    return 1, 0


def _farm_cells_for_metric_row(
    metric: str,
    month_start: dt.date,
    by_metric_month: dict[str, dict[dt.date, dict[str, dict[str, float | None]]]],
) -> dict[str, dict[str, float | None]]:
    farms = by_metric_month[metric].get(month_start, _empty_farm_cells())
    if metric != "beef_calf_sale":
        return {
            farm: dict(farms.get(farm, {"quantity": None, "unit_price": None}))
            for farm in HERD_FARM_OPTIONS
        }

    births_metric = BENCHMARK_METRICS[metric].get("births_metric")
    births_farms = (
        by_metric_month.get(births_metric, {}).get(month_start, _empty_farm_cells())
        if births_metric
        else _empty_farm_cells()
    )
    payload: dict[str, dict[str, float | None]] = {}
    for farm in HERD_FARM_OPTIONS:
        cells = dict(farms.get(farm, {"quantity": None, "unit_price": None}))
        birth_cells = births_farms.get(farm, {"quantity": None, "unit_price": None})
        cells["births"] = birth_cells.get("quantity")
        payload[farm] = cells
    return payload


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
        births = _parse_optional_float(row.get("births"))

        updated_delta, deleted_delta = _upsert_forecast_line(
            db,
            fiscal_year=fiscal_year,
            forecast_month=forecast_month,
            metric=metric,
            farm=farm,
            quantity=quantity,
            unit_price=unit_price,
            user_id=user_id,
        )
        updated += updated_delta
        deleted += deleted_delta

        births_metric = BENCHMARK_METRICS[metric].get("births_metric")
        if births_metric:
            b_updated, b_deleted = _upsert_forecast_line(
                db,
                fiscal_year=fiscal_year,
                forecast_month=forecast_month,
                metric=births_metric,
                farm=farm,
                quantity=births,
                unit_price=None,
                user_id=user_id,
            )
            updated += b_updated
            deleted += b_deleted

    db.commit()
    return {"metric": metric, "fiscal_year": fiscal_year, "updated": updated, "deleted": deleted}
