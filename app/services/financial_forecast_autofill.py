"""Populate financial forecast amounts from mapped benchmarking data sources."""

from __future__ import annotations

import datetime as dt
import gc
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, FinancialForecastLine, FinancialForecastMapping
from app.services.benchmarking import fiscal_year_months
from app.services.feed_purchase_forecasts import build_feed_purchase_forecasts_report
from app.services.financial_forecasts import (
    ensure_milk_deductions_data_source,
    ensure_milk_sales_data_source,
    ensure_stock_valuation_change_data_source,
    list_financial_mappings,
)
from app.services.hp_schedules import build_hp_payment_index
from app.services.milk_sales_forecasts import build_milk_sales_forecasts_report
from app.services.rental_agreements import build_rental_payment_index
from app.services.stock_sales_purchases_forecasts import (
    FORECAST_METRICS,
    build_stock_sales_purchases_forecasts_report,
)
from app.services.stock_valuation_forecasts import build_stock_valuation_change_index


@dataclass
class _DataSourceContext:
    milk: dict[tuple[str, dt.date], dict[str, float | None]]
    stock: dict[tuple[str, dt.date], dict[str, Any]]
    feed: dict[tuple[str, dt.date], dict[str, Any]]
    hp: dict[tuple[str, dt.date], dict[str, float]]
    rents: dict[tuple[str, dt.date], float]
    stock_valuations: dict[tuple[str, dt.date], float]

def _build_milk_index(report: dict[str, Any]) -> dict[tuple[str, dt.date], dict[str, float | None]]:
    index: dict[tuple[str, dt.date], dict[str, float | None]] = {}
    for row in report.get("rows", []):
        month = dt.date.fromisoformat(row["month_start"])
        for farm in HERD_FARM_OPTIONS:
            cells = row.get("farms", {}).get(farm, {})
            index[(farm, month)] = {
                "monthly_litres": cells.get("monthly_litres"),
                "daily_litres": cells.get("daily_litres"),
                "monthly_revenue": cells.get("monthly_revenue"),
                "monthly_deductions": cells.get("monthly_deductions"),
            }
    return index


def _build_stock_index(report: dict[str, Any]) -> dict[tuple[str, dt.date], dict[str, Any]]:
    index: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for row in report.get("rows", []):
        month = dt.date.fromisoformat(row["month_start"])
        for farm in HERD_FARM_OPTIONS:
            index[(farm, month)] = row.get("farms", {}).get(farm, {})
    return index


def _build_feed_index(report: dict[str, Any]) -> dict[tuple[str, dt.date], dict[str, Any]]:
    index: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for farm, payload in report.get("farms", {}).items():
        if farm not in HERD_FARM_OPTIONS:
            continue
        tables = payload.get("tables", {})
        concentrate_rows = tables.get("concentrate", {}).get("rows", [])
        straw_rows = tables.get("straw", {}).get("rows", [])
        forage_rows = tables.get("forage", {}).get("rows", [])
        for conc_row, straw_row, forage_row in zip(
            concentrate_rows, straw_rows, forage_rows
        ):
            month = dt.date.fromisoformat(conc_row["month_start"])
            index[(farm, month)] = {
                "concentrate_dairy": conc_row.get("dairy"),
                "concentrate_youngstock": conc_row.get("youngstock"),
                "straw": straw_row.get("total"),
                "forage": forage_row.get("total"),
                "detail": conc_row.get("detail", {}),
            }
    return index


# Light sources first; stock heads / valuations last so earlier prefixes can commit
# and free memory on the 512MB Render worker before the heavy builders run.
_PREFIX_FILL_ORDER: tuple[str, ...] = (
    "hp_schedules",
    "rents",
    "stock_sales_purchases",
    "milk_sales",
    "feed_purchases",
    "stock_valuations",
)


def _needed_source_prefixes(source_keys: list[str] | tuple[str, ...]) -> set[str]:
    prefixes: set[str] = set()
    for key in source_keys:
        if key.startswith("milk_sales."):
            prefixes.add("milk_sales")
        elif key.startswith("stock_sales_purchases."):
            prefixes.add("stock_sales_purchases")
        elif key.startswith("feed_purchases."):
            prefixes.add("feed_purchases")
        elif key.startswith("hp_schedules."):
            prefixes.add("hp_schedules")
        elif key.startswith("rents."):
            prefixes.add("rents")
        elif key.startswith("stock_valuations."):
            prefixes.add("stock_valuations")
    return prefixes


def _release_session_memory(db: Session) -> None:
    db.expire_all()
    gc.collect()


def _try_build(builder):
    try:
        return builder()
    except Exception:
        return None


def _build_data_source_context(
    db: Session,
    *,
    fiscal_year: int,
    today: dt.date | None,
    source_keys: list[str] | None = None,
) -> _DataSourceContext:
    need = _needed_source_prefixes(source_keys or [])
    build_all = not need

    milk: dict[tuple[str, dt.date], dict[str, float | None]] = {}
    if build_all or "milk_sales" in need:
        report = _try_build(
            lambda: build_milk_sales_forecasts_report(
                db, fiscal_year=fiscal_year, today=today
            )
        )
        if report:
            milk = _build_milk_index(report)
            del report
        _release_session_memory(db)

    stock: dict[tuple[str, dt.date], dict[str, Any]] = {}
    if build_all or "stock_sales_purchases" in need:
        report = _try_build(
            lambda: build_stock_sales_purchases_forecasts_report(
                db, fiscal_year=fiscal_year, today=today
            )
        )
        if report:
            stock = _build_stock_index(report)
            del report
        _release_session_memory(db)

    feed: dict[tuple[str, dt.date], dict[str, Any]] = {}
    if build_all or "feed_purchases" in need:
        report = _try_build(
            lambda: build_feed_purchase_forecasts_report(
                db, fiscal_year=fiscal_year, today=today
            )
        )
        if report:
            feed = _build_feed_index(report)
            del report
        _release_session_memory(db)

    hp: dict[tuple[str, dt.date], dict[str, float]] = {}
    if build_all or "hp_schedules" in need:
        built = _try_build(lambda: build_hp_payment_index(db, fiscal_year=fiscal_year))
        if built:
            hp = built
        _release_session_memory(db)

    rents: dict[tuple[str, dt.date], float] = {}
    if build_all or "rents" in need:
        built = _try_build(
            lambda: build_rental_payment_index(db, fiscal_year=fiscal_year)
        )
        if built:
            rents = built
        _release_session_memory(db)

    stock_valuations: dict[tuple[str, dt.date], float] = {}
    if build_all or "stock_valuations" in need:
        built = _try_build(
            lambda: build_stock_valuation_change_index(
                db, fiscal_year=fiscal_year, today=today
            )
        )
        if built:
            stock_valuations = built
        _release_session_memory(db)

    return _DataSourceContext(
        milk=milk,
        stock=stock,
        feed=feed,
        hp=hp,
        rents=rents,
        stock_valuations=stock_valuations,
    )


def resolve_data_source_value(
    source_key: str,
    *,
    farm: str,
    month: dt.date,
    ctx: _DataSourceContext,
) -> float | None:
    if source_key.startswith("milk_sales."):
        field = source_key.removeprefix("milk_sales.")
        return ctx.milk.get((farm, month), {}).get(field)

    if source_key.startswith("stock_sales_purchases."):
        suffix = source_key.removeprefix("stock_sales_purchases.")
        cell = ctx.stock.get((farm, month), {})
        if suffix == "sales_total":
            return cell.get("sales")
        if suffix == "purchases_total":
            return cell.get("purchases")
        if suffix in FORECAST_METRICS:
            return cell.get("detail", {}).get(suffix)
        return None

    if source_key.startswith("feed_purchases."):
        suffix = source_key.removeprefix("feed_purchases.")
        cell = ctx.feed.get((farm, month), {})
        if suffix == "concentrate_dairy":
            return cell.get("concentrate_dairy")
        if suffix == "concentrate_youngstock":
            return cell.get("concentrate_youngstock")
        if suffix == "straw":
            return cell.get("straw")
        if suffix == "forage":
            return cell.get("forage")
        if suffix.startswith("detail."):
            line_key = suffix.removeprefix("detail.")
            return cell.get("detail", {}).get(line_key)
        return None

    if source_key.startswith("hp_schedules."):
        field = source_key.removeprefix("hp_schedules.")
        cell = ctx.hp.get((farm, month.replace(day=1)), {})
        if field in ("monthly_capital", "monthly_interest", "monthly_payment"):
            return cell.get(field)
        return None

    if source_key.startswith("rents."):
        field = source_key.removeprefix("rents.")
        if field == "monthly_total":
            return ctx.rents.get((farm, month.replace(day=1)))
        return None

    if source_key.startswith("stock_valuations."):
        field = source_key.removeprefix("stock_valuations.")
        if field == "monthly_change":
            return ctx.stock_valuations.get((farm, month.replace(day=1)))
        return None

    return None


def _sum_source_values(
    source_keys: list[str],
    *,
    farm: str,
    month: dt.date,
    ctx: _DataSourceContext,
) -> float | None:
    values: list[float] = []
    for key in source_keys:
        value = resolve_data_source_value(key, farm=farm, month=month, ctx=ctx)
        if value is not None:
            values.append(float(value))
    if not values:
        return None
    return round(sum(values))


def _write_mapping_amounts(
    db: Session,
    *,
    mappings: list[dict[str, Any]],
    months: list[dt.date],
    target_farms: list[str],
    fiscal_year: int,
    fill_mode: str,
    user_id: int | None,
    ctx: _DataSourceContext,
) -> tuple[int, int, set[int]]:
    updated = 0
    skipped = 0
    mappings_filled: set[int] = set()

    for mapping in mappings:
        mapping_id = mapping["id"]
        source_keys = mapping["data_sources"]
        for month in months:
            for farm in target_farms:
                amount = _sum_source_values(
                    source_keys, farm=farm, month=month, ctx=ctx
                )
                if amount is None:
                    skipped += 1
                    continue

                existing = db.scalar(
                    select(FinancialForecastLine).where(
                        FinancialForecastLine.fiscal_year == fiscal_year,
                        FinancialForecastLine.forecast_month == month,
                        FinancialForecastLine.mapping_id == mapping_id,
                        FinancialForecastLine.farm == farm,
                    )
                )
                if (
                    fill_mode == "fill_empty"
                    and existing is not None
                    and existing.amount is not None
                ):
                    skipped += 1
                    continue

                if existing is None:
                    db.add(
                        FinancialForecastLine(
                            fiscal_year=fiscal_year,
                            forecast_month=month,
                            mapping_id=mapping_id,
                            farm=farm,
                            amount=amount,
                            updated_by_user_id=user_id,
                        )
                    )
                else:
                    existing.amount = amount
                    existing.updated_by_user_id = user_id
                updated += 1
                mappings_filled.add(mapping_id)

    return updated, skipped, mappings_filled


def fill_financial_forecasts_from_data_sources(
    db: Session,
    *,
    fiscal_year: int,
    farms: list[str] | None = None,
    fill_mode: str = "replace",
    user_id: int | None = None,
    today: dt.date | None = None,
    source_prefixes: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Write monthly amounts for mappings that have data sources configured."""
    if fill_mode not in ("replace", "fill_empty"):
        raise ValueError("fill_mode must be 'replace' or 'fill_empty'")

    target_farms = farms or list(HERD_FARM_OPTIONS)
    for farm in target_farms:
        if farm not in HERD_FARM_OPTIONS:
            raise ValueError(f"Unknown farm: {farm}")

    months = fiscal_year_months(fiscal_year)
    ensure_milk_sales_data_source(db)
    ensure_milk_deductions_data_source(db)
    ensure_stock_valuation_change_data_source(db)
    mappings = [row for row in list_financial_mappings(db) if row.get("data_sources")]
    if source_prefixes:
        mappings = [
            row
            for row in mappings
            if any(
                str(key).startswith(prefix)
                for key in row["data_sources"]
                for prefix in source_prefixes
            )
        ]
    if not mappings:
        return {"updated": 0, "mappings_filled": 0, "skipped": 0}

    needed = _needed_source_prefixes(
        [key for mapping in mappings for key in (mapping.get("data_sources") or [])]
    )
    prefix_order = [prefix for prefix in _PREFIX_FILL_ORDER if prefix in needed]

    updated = 0
    skipped = 0
    mappings_filled: set[int] = set()

    for prefix in prefix_order:
        subset = [
            row
            for row in mappings
            if any(
                str(key).startswith(f"{prefix}.")
                for key in (row.get("data_sources") or [])
            )
        ]
        if not subset:
            continue
        source_keys = [
            key for mapping in subset for key in (mapping.get("data_sources") or [])
        ]
        ctx = _build_data_source_context(
            db, fiscal_year=fiscal_year, today=today, source_keys=source_keys
        )
        prefix_updated, prefix_skipped, prefix_filled = _write_mapping_amounts(
            db,
            mappings=subset,
            months=months,
            target_farms=target_farms,
            fiscal_year=fiscal_year,
            fill_mode=fill_mode,
            user_id=user_id,
            ctx=ctx,
        )
        db.commit()
        del ctx
        _release_session_memory(db)
        updated += prefix_updated
        skipped += prefix_skipped
        mappings_filled.update(prefix_filled)

    return {
        "updated": updated,
        "mappings_filled": len(mappings_filled),
        "skipped": skipped,
    }


def refresh_milk_sales_financial_forecasts(
    db: Session,
    *,
    fiscal_year: int,
    user_id: int | None = None,
    today: dt.date | None = None,
) -> dict[str, int]:
    """Update monthly budget Milk Sales / Deductions from livestock milk price and yield."""
    return fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=fiscal_year,
        fill_mode="replace",
        user_id=user_id,
        today=today,
        source_prefixes=("milk_sales.",),
    )
