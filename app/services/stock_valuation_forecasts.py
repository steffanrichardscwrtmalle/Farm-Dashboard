"""Stock valuation forecasts: actual valuations plus projected values at fixed £/head."""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS
from app.services.benchmarking import available_fiscal_years
from app.services.events_common import (
    _fiscal_year_from_date,
    _iter_month_starts,
    normalize_farms,
)
from app.services.stock_forecasts import (
    build_stock_forecast_heads_index,
    resolve_stock_forecast_window,
)
from app.services.stock_valuations import build_stock_valuations_report

logger = logging.getLogger(__name__)

CATEGORY_DISPLAY_ORDER: tuple[str, ...] = ("Dairy", "Youngstock", "Beef")


def monthly_valuation_change_gbp(farm_view: dict[str, Any]) -> float:
    """P&L valuation change for a farm/month: closing − opening.

    An increase in herd value is a gain (positive) and is added in
    Sales + Valuation Change − other costs.
    """
    opening = float(farm_view.get("opening_grand_total_gbp") or 0)
    closing = float(farm_view.get("closing_grand_total_gbp") or 0)
    return closing - opening


def build_stock_valuation_change_index_from_report(
    report: dict[str, Any],
) -> dict[tuple[str, dt.date], float]:
    """Index (farm, month_start) → valuation change £ (closing − opening)."""
    index: dict[tuple[str, dt.date], float] = {}
    for row in report.get("rows", []):
        month = dt.date.fromisoformat(row["month_start"])
        totals = row.get("totals") or {}
        for farm in HERD_FARM_OPTIONS:
            farm_view = totals.get(farm)
            if not farm_view:
                continue
            opening = float(farm_view.get("opening_grand_total_gbp") or 0)
            closing = float(farm_view.get("closing_grand_total_gbp") or 0)
            # Skip months with no valuation footprint so autofill does not write zeros.
            if opening == 0 and closing == 0:
                continue
            index[(farm, month)] = closing - opening
    return index


def build_stock_valuation_change_index(
    db: Session,
    *,
    fiscal_year: int | None = None,
    today: dt.date | None = None,
) -> dict[tuple[str, dt.date], float]:
    """Monthly total valuation change (£) per farm for financial forecast autofill."""
    report = build_stock_valuation_forecasts_report(
        db,
        farms=list(HERD_FARM_OPTIONS),
        fiscal_year=fiscal_year,
        today=today,
    )
    return build_stock_valuation_change_index_from_report(report)


def _subtract_month(value: dt.date) -> dt.date:
    if value.month == 1:
        return dt.date(value.year - 1, 12, 1)
    return dt.date(value.year, value.month - 1, 1)


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _last_day_of_month(month_start: dt.date) -> dt.date:
    if month_start.month == 12:
        next_month = dt.date(month_start.year + 1, 1, 1)
    else:
        next_month = dt.date(month_start.year, month_start.month + 1, 1)
    return next_month - dt.timedelta(days=1)


def _empty_snapshot() -> dict[str, int | float]:
    return {"count": 0, "value_gbp": 0, "avg_value_gbp": 0}


def _count_for_category(farm_totals: dict[str, Any], category: str) -> int:
    if category == "Dairy":
        return int(farm_totals.get("dairy_cows", 0))
    return int(
        farm_totals.get("categories", {}).get(category, {}).get("count", 0)
    )


def _closing_category_snapshot(
    farm_totals: dict[str, Any],
    category: str,
) -> dict[str, int | float]:
    cats = farm_totals.get("categories", {})
    cat_data = cats.get(category, {})
    count = _count_for_category(farm_totals, category)
    value_gbp = round(float(cat_data.get("value_gbp", 0)), 0)
    avg = int(cat_data.get("avg_value_gbp", 0))
    if count and not avg:
        avg = math.floor(value_gbp / count)
    return {"count": count, "value_gbp": value_gbp, "avg_value_gbp": avg}


def _farm_view_from_valuations(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    categories: dict[str, dict[str, dict[str, int | float]]] = {}
    for category in CATEGORY_DISPLAY_ORDER:
        closing = _closing_category_snapshot(current, category)
        opening = (
            _closing_category_snapshot(previous, category)
            if previous is not None
            else _empty_snapshot()
        )
        categories[category] = {"opening": opening, "closing": closing}

    opening_grand = sum(
        categories[cat]["opening"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    closing_grand = sum(
        categories[cat]["closing"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    opening_animals = sum(
        categories[cat]["opening"]["count"] for cat in CATEGORY_DISPLAY_ORDER
    )
    closing_animals = sum(
        categories[cat]["closing"]["count"] for cat in CATEGORY_DISPLAY_ORDER
    )
    return {
        "categories": categories,
        "opening_grand_total_gbp": round(opening_grand, 0),
        "closing_grand_total_gbp": round(closing_grand, 0),
        "opening_total_animals": opening_animals,
        "closing_total_animals": closing_animals,
        "dairy_cows_opening": int(categories["Dairy"]["opening"]["count"]),
        "dairy_cows_closing": int(categories["Dairy"]["closing"]["count"]),
    }


def _projected_category(count: int, fixed_avg: int) -> dict[str, int | float]:
    value_gbp = count * fixed_avg
    return {
        "count": count,
        "value_gbp": value_gbp,
        "avg_value_gbp": fixed_avg if count else 0,
    }


def _closing_counts_by_category(farm_totals: dict[str, Any]) -> dict[str, int]:
    return {
        category: int(_closing_category_snapshot(farm_totals, category)["count"])
        for category in CATEGORY_DISPLAY_ORDER
    }


def _forecast_deltas_by_category(
    heads: dict[str, dict[str, int]],
) -> dict[str, int]:
    return {
        category: int(heads.get(category, {}).get("closing", 0))
        - int(heads.get(category, {}).get("opening", 0))
        for category in CATEGORY_DISPLAY_ORDER
    }


def _farm_view_from_counts_and_deltas(
    opening_counts: dict[str, int],
    deltas: dict[str, int],
    fixed_rates: dict[str, int],
) -> tuple[dict[str, Any], dict[str, int]]:
    categories: dict[str, dict[str, dict[str, int | float]]] = {}
    closing_counts: dict[str, int] = {}
    for category in CATEGORY_DISPLAY_ORDER:
        opening_count = opening_counts.get(category, 0)
        closing_count = opening_count + deltas.get(category, 0)
        fixed_avg = int(fixed_rates.get(category, 0))
        categories[category] = {
            "opening": _projected_category(opening_count, fixed_avg),
            "closing": _projected_category(closing_count, fixed_avg),
        }
        closing_counts[category] = closing_count

    opening_grand = sum(
        categories[cat]["opening"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    closing_grand = sum(
        categories[cat]["closing"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    opening_animals = sum(opening_counts.get(cat, 0) for cat in CATEGORY_DISPLAY_ORDER)
    closing_animals = sum(closing_counts.get(cat, 0) for cat in CATEGORY_DISPLAY_ORDER)
    return {
        "categories": categories,
        "opening_grand_total_gbp": opening_grand,
        "closing_grand_total_gbp": closing_grand,
        "opening_total_animals": opening_animals,
        "closing_total_animals": closing_animals,
        "dairy_cows_opening": int(categories["Dairy"]["opening"]["count"]),
        "dairy_cows_closing": int(categories["Dairy"]["closing"]["count"]),
    }, closing_counts


def _empty_counts() -> dict[str, int]:
    return {category: 0 for category in CATEGORY_DISPLAY_ORDER}


def _seed_opening_counts_from_prior_month(
    farm: str,
    month_start: dt.date,
    fy_start_month: dt.date,
    val_by_month: dict[str, dict[str, Any]],
    prior_month_totals: dict[str, dict[str, Any]],
) -> dict[str, int]:
    prev_month = _subtract_month(month_start)
    prev_iso = prev_month.isoformat()
    if prev_iso in val_by_month:
        farm_totals = val_by_month[prev_iso].get("totals", {}).get(farm, {})
        return _closing_counts_by_category(farm_totals)
    if month_start == fy_start_month:
        return _closing_counts_by_category(prior_month_totals.get(farm, {}))
    return _empty_counts()


def _fixed_rates_from_totals(
    totals_by_farm: dict[str, Any],
    farms: list[str],
    month_label: str | None,
) -> tuple[dict[str, dict[str, int]], str | None]:
    fixed: dict[str, dict[str, int]] = {}
    for farm in farms:
        farm_totals = totals_by_farm.get(farm, {})
        fixed[farm] = {
            category: int(
                _closing_category_snapshot(farm_totals, category)["avg_value_gbp"]
            )
            for category in CATEGORY_DISPLAY_ORDER
        }
    return fixed, month_label


def _opening_counts_from_heads(
    farm_heads: dict[str, dict[str, dict[str, int]]],
    month_iso: str,
) -> dict[str, int] | None:
    month_heads = farm_heads.get(month_iso) or {}
    if not month_heads:
        return None
    counts = {
        category: int((month_heads.get(category) or {}).get("opening", 0))
        for category in CATEGORY_DISPLAY_ORDER
        if category in month_heads
    }
    return counts or None


def _roll_projected_month(
    *,
    farm: str,
    month_iso: str,
    opening_counts: dict[str, int],
    forecast_heads: dict[str, dict[str, dict[str, dict[str, int]]]],
    fixed_rates: dict[str, dict[str, int]],
) -> tuple[dict[str, Any], dict[str, int]]:
    heads = forecast_heads.get(farm, {}).get(month_iso, {})
    deltas = _forecast_deltas_by_category(heads)
    return _farm_view_from_counts_and_deltas(
        opening_counts,
        deltas,
        fixed_rates.get(farm, {}),
    )


def _merge_farm_views(farm_views: dict[str, dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, dict[str, dict[str, int | float]]] = {}
    for category in CATEGORY_DISPLAY_ORDER:
        opening_count = sum(
            view["categories"][category]["opening"]["count"]
            for view in farm_views.values()
        )
        opening_value = sum(
            view["categories"][category]["opening"]["value_gbp"]
            for view in farm_views.values()
        )
        closing_count = sum(
            view["categories"][category]["closing"]["count"]
            for view in farm_views.values()
        )
        closing_value = sum(
            view["categories"][category]["closing"]["value_gbp"]
            for view in farm_views.values()
        )
        categories[category] = {
            "opening": {
                "count": opening_count,
                "value_gbp": round(opening_value, 0),
                "avg_value_gbp": (
                    math.floor(opening_value / opening_count) if opening_count else 0
                ),
            },
            "closing": {
                "count": closing_count,
                "value_gbp": round(closing_value, 0),
                "avg_value_gbp": (
                    math.floor(closing_value / closing_count) if closing_count else 0
                ),
            },
        }

    opening_grand = sum(
        categories[cat]["opening"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    closing_grand = sum(
        categories[cat]["closing"]["value_gbp"] for cat in CATEGORY_DISPLAY_ORDER
    )
    return {
        "categories": categories,
        "opening_grand_total_gbp": round(opening_grand, 0),
        "closing_grand_total_gbp": round(closing_grand, 0),
        "opening_total_animals": sum(
            categories[cat]["opening"]["count"] for cat in CATEGORY_DISPLAY_ORDER
        ),
        "closing_total_animals": sum(
            categories[cat]["closing"]["count"] for cat in CATEGORY_DISPLAY_ORDER
        ),
        "dairy_cows_opening": sum(
            view["dairy_cows_opening"] for view in farm_views.values()
        ),
        "dairy_cows_closing": sum(
            view["dairy_cows_closing"] for view in farm_views.values()
        ),
    }


def _extract_fixed_rates(
    val_months: list[dict[str, Any]],
    farms: list[str],
    last_actual_month: dt.date,
) -> tuple[dict[str, dict[str, int]], str | None]:
    actual_months = [
        month
        for month in val_months
        if month["month_start"] <= last_actual_month.isoformat()
    ]
    if not actual_months:
        empty = {farm: {cat: 0 for cat in CATEGORY_DISPLAY_ORDER} for farm in farms}
        return empty, None

    latest = actual_months[-1]
    return _fixed_rates_from_totals(
        latest.get("totals", {}), farms, latest.get("month_label")
    )


def _load_prior_month_valuation_totals(
    db: Session,
    *,
    farms: list[str],
    prior_month: dt.date,
) -> dict[str, dict[str, Any]]:
    """Month before FY start belongs to the prior fiscal year."""
    prior_fy = _fiscal_year_from_date(prior_month)
    report = build_stock_valuations_report(
        db,
        farms=farms,
        fiscal_year=prior_fy,
        month_from=prior_month,
        month_to=_last_day_of_month(prior_month),
    )
    for month in report.get("months", []):
        if month["month_start"] == prior_month.isoformat():
            return month.get("totals", {})
    return {}


def build_stock_valuation_forecasts_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    fiscal_year: int | None = None,
    today: dt.date | None = None,
    shared: Any | None = None,
    forecast_heads: dict[str, dict[str, dict[str, dict[str, int]]]] | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    reference_today = today or dt.date.today()
    current_month = _month_start(reference_today)
    last_actual_month = _subtract_month(current_month)

    year_options = available_fiscal_years()
    if shared is not None:
        range_start_month = shared.range_start_month
        range_end_month = shared.range_end_month
        slider_min = shared.slider_min_month
        slider_max = shared.slider_max_month
    else:
        range_start_month, range_end_month, slider_min, slider_max = (
            resolve_stock_forecast_window(
                fiscal_year=fiscal_year,
                month_from=month_from,
                month_to=month_to,
            )
        )
    prev_range_month = _subtract_month(range_start_month)

    empty: dict[str, Any] = {
        "rows": [],
        "fiscal_year_options": year_options,
        "selected_fiscal_year": fiscal_year,
        "any_year": fiscal_year is None,
        "month_from": range_start_month.isoformat(),
        "month_to": range_end_month.isoformat(),
        "actual_cutoff": last_actual_month.isoformat(),
        "projected_from": current_month.isoformat(),
        "fixed_rates": {},
        "fixed_rates_month": None,
        "date_bounds": {
            "min": slider_min.isoformat(),
            "max": _last_day_of_month(slider_max).isoformat(),
        },
    }
    if not selected_farms:
        return empty

    val_report = build_stock_valuations_report(
        db,
        farms=selected_farms,
        fiscal_year="any" if fiscal_year is None else fiscal_year,
        month_from=range_start_month,
        month_to=_last_day_of_month(range_end_month),
    )
    val_by_month = {
        month["month_start"]: month for month in val_report.get("months", [])
    }
    prior_month_totals = _load_prior_month_valuation_totals(
        db,
        farms=selected_farms,
        prior_month=prev_range_month,
    )
    last_actual_iso = last_actual_month.isoformat()
    if last_actual_iso in val_by_month:
        last_actual_totals = val_by_month[last_actual_iso].get("totals", {})
    else:
        last_actual_totals = _load_prior_month_valuation_totals(
            db,
            farms=selected_farms,
            prior_month=last_actual_month,
        )

    fixed_rates, fixed_rates_month = _extract_fixed_rates(
        val_report.get("months", []),
        selected_farms,
        last_actual_month,
    )
    if not fixed_rates_month and last_actual_totals:
        fixed_rates, fixed_rates_month = _fixed_rates_from_totals(
            last_actual_totals,
            selected_farms,
            last_actual_month.strftime("%b-%y"),
        )

    if forecast_heads is None:
        try:
            forecast_heads = build_stock_forecast_heads_index(
                db,
                farms=selected_farms,
                fiscal_year=fiscal_year,
                today=reference_today,
                shared=shared,
                month_from=month_from,
                month_to=month_to,
            )
        except Exception:
            logger.exception(
                "Stock forecast heads failed for FY %s",
                fiscal_year if fiscal_year is not None else "any",
            )
            forecast_heads = {farm: {} for farm in selected_farms}

    rolling_closing: dict[str, dict[str, int]] = {}
    seed_totals = last_actual_totals or prior_month_totals
    for farm in selected_farms:
        if seed_totals.get(farm):
            rolling_closing[farm] = _closing_counts_by_category(seed_totals[farm])

    # Next FY must open at the previous FY's projected closing, not last actual.
    if range_start_month > current_month:
        first_iso = range_start_month.isoformat()
        for farm in selected_farms:
            from_heads = _opening_counts_from_heads(
                forecast_heads.get(farm, {}), first_iso
            )
            if from_heads:
                base = dict(rolling_closing.get(farm) or _empty_counts())
                base.update(from_heads)
                rolling_closing[farm] = base

    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(range_start_month, range_end_month):
        month_iso = month_start.isoformat()
        month_label = month_start.strftime("%b-%y")
        is_projected = month_start >= current_month
        source = "projected" if is_projected else "actual"

        farm_totals: dict[str, Any] = {}
        val_month = val_by_month.get(month_iso) if not is_projected else None
        if not is_projected and val_month is None:
            # Keep the month in the table; roll counts if this snapshot is missing.
            is_projected = True
            source = "projected"
        if not is_projected:
            prev_month = _subtract_month(month_start)
            prev_val_month = val_by_month.get(prev_month.isoformat())
            if prev_val_month is not None:
                prev_farm_totals = prev_val_month.get("totals", {})
            elif month_start == range_start_month:
                prev_farm_totals = prior_month_totals
            else:
                prev_farm_totals = {}
            for farm in selected_farms:
                current = val_month.get("totals", {}).get(farm, {})
                previous = prev_farm_totals.get(farm)
                farm_totals[farm] = _farm_view_from_valuations(current, previous)
                rolling_closing[farm] = _closing_counts_by_category(current)
        else:
            for farm in selected_farms:
                opening_counts = rolling_closing.get(farm)
                if opening_counts is None:
                    opening_counts = _seed_opening_counts_from_prior_month(
                        farm,
                        month_start,
                        range_start_month,
                        val_by_month,
                        prior_month_totals or last_actual_totals,
                    )
                view, rolling_closing[farm] = _roll_projected_month(
                    farm=farm,
                    month_iso=month_iso,
                    opening_counts=opening_counts,
                    forecast_heads=forecast_heads,
                    fixed_rates=fixed_rates,
                )
                farm_totals[farm] = view

        if len(selected_farms) > 1:
            farm_totals["all"] = _merge_farm_views(
                {farm: farm_totals[farm] for farm in selected_farms}
            )

        display_totals = (
            farm_totals["all"]
            if len(selected_farms) > 1
            else farm_totals[selected_farms[0]]
        )
        rows.append(
            {
                "month_start": month_iso,
                "month_label": month_label,
                "source": source,
                "totals": farm_totals,
                "opening_grand_total_gbp": display_totals["opening_grand_total_gbp"],
                "closing_grand_total_gbp": display_totals["closing_grand_total_gbp"],
            }
        )

    return {
        "rows": rows,
        "fiscal_year_options": year_options,
        "selected_fiscal_year": fiscal_year,
        "any_year": fiscal_year is None,
        "month_from": range_start_month.isoformat(),
        "month_to": range_end_month.isoformat(),
        "actual_cutoff": last_actual_month.isoformat(),
        "projected_from": current_month.isoformat(),
        "fixed_rates": fixed_rates,
        "fixed_rates_month": fixed_rates_month,
        "date_bounds": {
            "min": slider_min.isoformat(),
            "max": _last_day_of_month(slider_max).isoformat(),
        },
    }
