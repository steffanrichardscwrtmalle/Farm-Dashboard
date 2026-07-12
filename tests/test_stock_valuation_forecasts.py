"""Tests for stock valuation forecasts."""

from __future__ import annotations

import datetime as dt

from app.services.stock_valuation_forecasts import (
    CATEGORY_DISPLAY_ORDER,
    _extract_fixed_rates,
    _farm_view_from_counts_and_deltas,
    _farm_view_from_valuations,
    _merge_farm_views,
    _projected_category,
    build_stock_valuation_change_index_from_report,
    monthly_valuation_change_gbp,
)

LAST_ACTUAL = dt.date(2026, 6, 1)


def test_projected_category_uses_fixed_avg() -> None:
    result = _projected_category(50, 2150)
    assert result == {"count": 50, "value_gbp": 107500, "avg_value_gbp": 2150}

    empty = _projected_category(0, 2150)
    assert empty["count"] == 0
    assert empty["value_gbp"] == 0
    assert empty["avg_value_gbp"] == 0


def test_projected_view_applies_deltas_to_opening_counts() -> None:
    opening_counts = {"Dairy": 100, "Youngstock": 40, "Beef": 5}
    deltas = {"Dairy": -2, "Youngstock": 2, "Beef": -1}
    fixed = {"Dairy": 2150, "Youngstock": 800, "Beef": 600}
    view, closing_counts = _farm_view_from_counts_and_deltas(
        opening_counts, deltas, fixed
    )

    assert view["categories"]["Dairy"]["opening"]["count"] == 100
    assert view["categories"]["Dairy"]["closing"]["count"] == 98
    assert view["categories"]["Beef"]["opening"]["count"] == 5
    assert view["categories"]["Beef"]["closing"]["count"] == 4
    assert closing_counts["Dairy"] == 98


def test_projected_months_chain_opening_from_prior_closing() -> None:
    fixed = {"Dairy": 2150, "Youngstock": 800, "Beef": 600}
    june_closing = {"Dairy": 98, "Youngstock": 42, "Beef": 4}
    july_delta = {"Dairy": -1, "Youngstock": 0, "Beef": 0}

    july_view, july_closing = _farm_view_from_counts_and_deltas(
        june_closing, july_delta, fixed
    )
    assert july_view["categories"]["Dairy"]["opening"]["count"] == 98
    assert july_view["categories"]["Dairy"]["closing"]["count"] == 97

    august_view, _ = _farm_view_from_counts_and_deltas(
        july_closing, {"Dairy": 0, "Youngstock": 1, "Beef": 0}, fixed
    )
    assert august_view["categories"]["Dairy"]["opening"]["count"] == 97
    assert august_view["categories"]["Youngstock"]["closing"]["count"] == 43


def test_fy_start_actual_opening_from_prior_month_valuation() -> None:
    prior = {
        "dairy_cows": 100,
        "categories": {
            "Dairy": {"count": 100, "value_gbp": 215000, "avg_value_gbp": 2150},
            "Youngstock": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
            "Beef": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
        },
    }
    current = {
        "dairy_cows": 98,
        "categories": {
            "Dairy": {"count": 98, "value_gbp": 210700, "avg_value_gbp": 2150},
            "Youngstock": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
            "Beef": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
        },
    }
    view = _farm_view_from_valuations(current, prior)
    assert view["categories"]["Dairy"]["opening"]["count"] == 100
    assert view["categories"]["Dairy"]["opening"]["value_gbp"] == 215000


def test_merge_farm_views_sums_farms() -> None:
    def _single_farm(cows: int, avg: int) -> dict:
        value = cows * avg
        return {
            "categories": {
                "Dairy": {
                    "opening": {"count": cows, "value_gbp": value, "avg_value_gbp": avg},
                    "closing": {"count": cows, "value_gbp": value, "avg_value_gbp": avg},
                },
                "Youngstock": {
                    "opening": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                    "closing": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                },
                "Beef": {
                    "opening": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                    "closing": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                },
            },
            "opening_grand_total_gbp": value,
            "closing_grand_total_gbp": value,
            "opening_total_animals": cows,
            "closing_total_animals": cows,
            "dairy_cows_opening": cows,
            "dairy_cows_closing": cows,
        }

    merged = _merge_farm_views(
        {
            "CM": _single_farm(100, 2150),
            "GAD": _single_farm(120, 2210),
        }
    )
    assert merged["dairy_cows_closing"] == 220
    assert merged["categories"]["Dairy"]["closing"]["value_gbp"] == 215000 + 265200
    assert merged["categories"]["Dairy"]["closing"]["avg_value_gbp"] == 2182


def test_extract_fixed_rates_uses_latest_actual_month() -> None:
    months = [
        {
            "month_start": "2026-04-01",
            "month_label": "Apr-26",
            "totals": {
                "CM": {
                    "dairy_cows": 100,
                    "categories": {
                        "Dairy": {"count": 100, "value_gbp": 200000, "avg_value_gbp": 2000},
                        "Youngstock": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                        "Beef": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                    },
                }
            },
        },
        {
            "month_start": "2026-06-01",
            "month_label": "Jun-26",
            "totals": {
                "CM": {
                    "dairy_cows": 98,
                    "categories": {
                        "Dairy": {"count": 98, "value_gbp": 210700, "avg_value_gbp": 2150},
                        "Youngstock": {"count": 40, "value_gbp": 32000, "avg_value_gbp": 800},
                        "Beef": {"count": 10, "value_gbp": 6000, "avg_value_gbp": 600},
                    },
                },
                "GAD": {
                    "dairy_cows": 120,
                    "categories": {
                        "Dairy": {"count": 120, "value_gbp": 265200, "avg_value_gbp": 2210},
                        "Youngstock": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                        "Beef": {"count": 0, "value_gbp": 0, "avg_value_gbp": 0},
                    },
                },
            },
        },
    ]
    fixed, label = _extract_fixed_rates(months, ["CM", "GAD"], LAST_ACTUAL)
    assert label == "Jun-26"
    assert fixed["CM"]["Dairy"] == 2150
    assert fixed["GAD"]["Dairy"] == 2210
    assert fixed["CM"]["Youngstock"] == 800
    for category in CATEGORY_DISPLAY_ORDER:
        assert category in fixed["CM"]
        assert category in fixed["GAD"]


def test_monthly_valuation_change_is_closing_minus_opening() -> None:
    view = {
        "opening_grand_total_gbp": 100_000,
        "closing_grand_total_gbp": 97_500,
    }
    assert monthly_valuation_change_gbp(view) == -2_500


def test_valuation_change_index_per_farm_month() -> None:
    report = {
        "rows": [
            {
                "month_start": "2026-07-01",
                "totals": {
                    "CM": {
                        "opening_grand_total_gbp": 200_000,
                        "closing_grand_total_gbp": 205_000,
                    },
                    "GAD": {
                        "opening_grand_total_gbp": 150_000,
                        "closing_grand_total_gbp": 148_000,
                    },
                },
            }
        ]
    }
    index = build_stock_valuation_change_index_from_report(report)
    assert index[("CM", dt.date(2026, 7, 1))] == 5_000
    assert index[("GAD", dt.date(2026, 7, 1))] == -2_000
    empty_report = {
        "rows": [
            {
                "month_start": "2026-08-01",
                "totals": {
                    "CM": {
                        "opening_grand_total_gbp": 0,
                        "closing_grand_total_gbp": 0,
                    },
                },
            }
        ]
    }
    assert build_stock_valuation_change_index_from_report(empty_report) == {}
