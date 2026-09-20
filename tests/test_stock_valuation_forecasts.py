"""Tests for stock valuation forecasts."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

from app.services.stock_valuation_forecasts import (
    CATEGORY_DISPLAY_ORDER,
    _extract_fixed_rates,
    _farm_view_from_counts_and_deltas,
    _farm_view_from_valuations,
    _merge_farm_views,
    _projected_category,
    build_stock_valuation_change_index_from_report,
    build_stock_valuation_forecasts_report,
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


def _farm_totals(
    *,
    dairy: int,
    dairy_avg: int,
    youngstock: int = 0,
    young_avg: int = 0,
    beef: int = 0,
    beef_avg: int = 0,
) -> dict:
    categories = {
        "Dairy": {
            "count": dairy,
            "value_gbp": dairy * dairy_avg,
            "avg_value_gbp": dairy_avg if dairy else 0,
        },
        "Youngstock": {
            "count": youngstock,
            "value_gbp": youngstock * young_avg,
            "avg_value_gbp": young_avg if youngstock else 0,
        },
        "Beef": {
            "count": beef,
            "value_gbp": beef * beef_avg,
            "avg_value_gbp": beef_avg if beef else 0,
        },
    }
    return {
        "dairy_cows": dairy,
        "categories": categories,
        "grand_total_gbp": sum(cat["value_gbp"] for cat in categories.values()),
        "total_animals": dairy + youngstock + beef,
    }


def test_future_fy_populates_when_livestock_tables_are_blank(monkeypatch) -> None:
    """Next FY used to start at a zero herd and skip autofill."""
    today = dt.date(2026, 9, 20)
    last_actual = dt.date(2026, 8, 1)
    cm = _farm_totals(dairy=100, dairy_avg=2000)
    gad = _farm_totals(dairy=80, dairy_avg=2100)

    def fake_valuations(_db, **kwargs):
        month_from = kwargs.get("month_from")
        if month_from == last_actual:
            return {
                "months": [
                    {
                        "month_start": "2026-08-01",
                        "month_label": "Aug-26",
                        "totals": {"CM": cm, "GAD": gad},
                    }
                ]
            }
        return {"months": []}

    def fake_heads(_db, **kwargs):
        return {"CM": {}, "GAD": {}}

    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_valuations_report",
        fake_valuations,
    )
    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_forecast_heads_index",
        fake_heads,
    )

    report = build_stock_valuation_forecasts_report(
        MagicMock(),
        farms=["CM", "GAD"],
        fiscal_year=2028,
        today=today,
    )
    april = next(row for row in report["rows"] if row["month_start"] == "2027-04-01")
    assert april["source"] == "projected"
    assert april["totals"]["CM"]["opening_grand_total_gbp"] == 200_000
    assert april["totals"]["CM"]["closing_grand_total_gbp"] == 200_000
    assert april["totals"]["GAD"]["opening_grand_total_gbp"] == 168_000

    index = build_stock_valuation_change_index_from_report(report)
    assert index[("CM", dt.date(2027, 4, 1))] == 0
    assert index[("GAD", dt.date(2027, 4, 1))] == 0


def test_future_fy_opening_uses_stock_forecast_heads_not_last_actual(
    monkeypatch,
) -> None:
    """YE 2028 April must continue YE 2027 projected closing, not last actual."""
    today = dt.date(2026, 9, 20)
    last_actual = dt.date(2026, 8, 1)
    gad_actual = _farm_totals(dairy=843, dairy_avg=2100)
    cm_actual = _farm_totals(dairy=100, dairy_avg=2000)

    def fake_valuations(_db, **kwargs):
        month_from = kwargs.get("month_from")
        if month_from == last_actual:
            return {
                "months": [
                    {
                        "month_start": "2026-08-01",
                        "month_label": "Aug-26",
                        "totals": {"CM": cm_actual, "GAD": gad_actual},
                    }
                ]
            }
        return {"months": []}

    def fake_heads(_db, **kwargs):
        # Stock forecasts walk Sep–Mar, so April opening is March's projected close.
        return {
            "CM": {
                "2027-04-01": {
                    "Dairy": {"opening": 98, "closing": 97},
                    "Youngstock": {"opening": 0, "closing": 0},
                    "Beef": {"opening": 0, "closing": 0},
                }
            },
            "GAD": {
                "2027-04-01": {
                    "Dairy": {"opening": 959, "closing": 961},
                    "Youngstock": {"opening": 0, "closing": 0},
                    "Beef": {"opening": 0, "closing": 0},
                }
            },
        }

    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_valuations_report",
        fake_valuations,
    )
    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_forecast_heads_index",
        fake_heads,
    )

    report = build_stock_valuation_forecasts_report(
        MagicMock(),
        farms=["CM", "GAD"],
        fiscal_year=2028,
        today=today,
    )
    april = next(row for row in report["rows"] if row["month_start"] == "2027-04-01")
    assert april["totals"]["GAD"]["categories"]["Dairy"]["opening"]["count"] == 959
    assert april["totals"]["GAD"]["categories"]["Dairy"]["closing"]["count"] == 961
    assert april["totals"]["CM"]["categories"]["Dairy"]["opening"]["count"] == 98
    assert april["totals"]["CM"]["categories"]["Dairy"]["closing"]["count"] == 97
    assert april["totals"]["GAD"]["opening_grand_total_gbp"] == 959 * 2100

    index = build_stock_valuation_change_index_from_report(report)
    assert index[("CM", dt.date(2027, 4, 1))] == -2_000


def test_actual_months_still_fill_when_heads_builder_fails(monkeypatch) -> None:
    today = dt.date(2026, 9, 20)
    july = _farm_totals(dairy=102, dairy_avg=2000)
    aug = _farm_totals(dairy=100, dairy_avg=2000)

    def fake_valuations(_db, **kwargs):
        fy = kwargs.get("fiscal_year")
        month_from = kwargs.get("month_from")
        if fy == 2027 and month_from == dt.date(2026, 4, 1):
            return {
                "months": [
                    {
                        "month_start": "2026-07-01",
                        "month_label": "Jul-26",
                        "totals": {"CM": july, "GAD": july},
                    },
                    {
                        "month_start": "2026-08-01",
                        "month_label": "Aug-26",
                        "totals": {"CM": aug, "GAD": aug},
                    },
                ]
            }
        if month_from == dt.date(2026, 8, 1):
            return {
                "months": [
                    {
                        "month_start": "2026-08-01",
                        "month_label": "Aug-26",
                        "totals": {"CM": aug, "GAD": aug},
                    }
                ]
            }
        return {"months": []}

    def fake_heads(*_args, **_kwargs):
        raise RuntimeError("livestock forecast table incomplete")

    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_valuations_report",
        fake_valuations,
    )
    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_forecast_heads_index",
        fake_heads,
    )

    report = build_stock_valuation_forecasts_report(
        MagicMock(),
        farms=["CM", "GAD"],
        fiscal_year=2027,
        today=today,
    )
    august = next(row for row in report["rows"] if row["month_start"] == "2026-08-01")
    assert august["source"] == "actual"
    assert august["totals"]["CM"]["closing_grand_total_gbp"] == 200_000

    september = next(row for row in report["rows"] if row["month_start"] == "2026-09-01")
    assert september["source"] == "projected"
    assert september["totals"]["CM"]["opening_grand_total_gbp"] == 200_000

    index = build_stock_valuation_change_index_from_report(report)
    assert ("CM", dt.date(2026, 8, 1)) in index
    assert ("CM", dt.date(2026, 9, 1)) in index


def test_any_year_valuation_spans_this_and_next_fy(monkeypatch) -> None:
    today = dt.date(2026, 7, 6)

    def fake_valuations(_db, **kwargs):
        return {"months": []}

    def fake_heads(_db, **kwargs):
        return {"CM": {}, "GAD": {}}

    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.available_fiscal_years",
        lambda: [2027, 2028],
    )
    monkeypatch.setattr(
        "app.services.stock_forecasts.available_fiscal_years",
        lambda: [2027, 2028],
    )
    monkeypatch.setattr(
        "app.services.benchmarking.available_fiscal_years",
        lambda: [2027, 2028],
    )
    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_valuations_report",
        fake_valuations,
    )
    monkeypatch.setattr(
        "app.services.stock_valuation_forecasts.build_stock_forecast_heads_index",
        fake_heads,
    )

    report = build_stock_valuation_forecasts_report(
        MagicMock(),
        farms=["CM", "GAD"],
        fiscal_year=None,
        today=today,
    )
    assert report["any_year"] is True
    assert report["selected_fiscal_year"] is None
    assert report["month_from"] == "2026-04-01"
    assert report["month_to"] == "2028-03-01"
    assert len(report["rows"]) == 24
    assert report["rows"][0]["month_start"] == "2026-04-01"
    assert report["rows"][-1]["month_start"] == "2028-03-01"
    assert report["date_bounds"]["min"] == "2026-04-01"
    assert report["date_bounds"]["max"] == "2028-03-31"
