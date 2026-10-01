"""Tests for Feedlync Loaded Mixes feed usage."""

from __future__ import annotations

import datetime as dt
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, FeedUsageRecord
from app.services.feed_usage import (
    build_usage_report,
    build_usage_xlsx,
    get_import_status,
    get_usage_report,
    import_feed_usage,
    import_feed_usage_months,
    import_previous_month_usage_if_due,
    previous_month_usage_import_month,
    resolve_usage_month,
    resolve_usage_period,
    usage_fiscal_year_options,
)
from app.services.feed_usage_settings import (
    assigned_ration_names,
    ingredient_inclusion_lookup,
    ration_farm_lookup,
    save_ingredient_assignment,
    save_ration_assignment,
    seed_ration_assignments_if_empty,
)
from app.services.feedlync_api import (
    _LOADED_MIX_INGREDIENT_PATH,
    _parse_ingredient_spends,
    _parse_ingredient_spends_by_farm,
    _utc_month_range,
    farm_for_usage_ration,
    fetch_loaded_mix_ingredient_usage,
    month_bounds,
    previous_calendar_month,
)


AUG_START = dt.date(2026, 8, 1)
AUG_END = dt.date(2026, 8, 31)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_month_bounds_full_calendar_month() -> None:
    start, end = month_bounds(2026, 8)
    assert start == AUG_START
    assert end == AUG_END


def test_previous_calendar_month_from_september() -> None:
    assert previous_calendar_month(dt.date(2026, 9, 7)) == AUG_START


def test_resolve_usage_month_defaults_to_last_month() -> None:
    assert resolve_usage_month(None, today=dt.date(2026, 9, 7)) == AUG_START
    assert resolve_usage_month("2026-08", today=dt.date(2026, 9, 7)) == AUG_START


def test_previous_month_usage_import_only_on_second() -> None:
    assert previous_month_usage_import_month(dt.date(2026, 9, 1)) is None
    assert previous_month_usage_import_month(dt.date(2026, 9, 2)) == AUG_START
    assert previous_month_usage_import_month(dt.date(2026, 9, 3)) is None
    assert previous_month_usage_import_month(dt.date(2026, 1, 2)) == dt.date(2025, 12, 1)


def test_import_previous_month_usage_if_due_skips_other_days(db: Session) -> None:
    with patch("app.services.feed_usage.import_feed_usage") as mocked:
        assert import_previous_month_usage_if_due(db, today=dt.date(2026, 9, 1)) is None
        mocked.assert_not_called()


def test_import_previous_month_usage_if_due_runs_on_second(db: Session) -> None:
    with patch(
        "app.services.feed_usage.import_feed_usage",
        return_value={"rows_imported": 4, "month": "2026-08"},
    ) as mocked:
        result = import_previous_month_usage_if_due(db, today=dt.date(2026, 9, 2))
    mocked.assert_called_once_with(db, month=AUG_START)
    assert result is not None
    assert result["month"] == "2026-08"


def test_utc_month_range_matches_feedlync_last_month_encoding() -> None:
    start, end = _utc_month_range(AUG_START, AUG_END)
    assert start == "2026-08-01T00:00:00.000Z"
    assert end == "2026-09-01T00:00:00.000Z"


def test_parse_ingredient_spends_sorts_and_merges() -> None:
    rows = _parse_ingredient_spends(
        [
            {
                "ingredientName": "Rape Meal",
                "quantity": 200000,
                "drymatterQuantity": 180000,
                "cost": 50000,
            },
            {
                "ingredientName": "Rape Meal",
                "quantity": 322593,
                "drymatterQuantity": 290000,
                "cost": 70000.25,
            },
            {
                "ingredientName": "Grass Silage",
                "quantity": 100,
                "drymatterQuantity": 30,
                "cost": 10,
            },
        ]
    )
    assert rows[0]["ingredient_name"] == "Rape Meal"
    assert rows[0]["as_fed_kg"] == 522593
    assert rows[0]["dm_kg"] == 470000
    assert rows[0]["cost"] == 120000.25
    assert rows[1]["ingredient_name"] == "Grass Silage"


def test_build_usage_report_totals_and_daily_averages() -> None:
    report = build_usage_report(
        [
            {
                "ingredient_name": "Grass Silage",
                    "as_fed_kg": 6_398_038,
                "dm_kg": 3_404_860,
                "cost": 872_640.75,
            },
            {
                "ingredient_name": "Rape Meal",
                "as_fed_kg": 522_593,
                "dm_kg": 470_000,
                "cost": 150_000,
            },
        ],
        period_start=AUG_START,
        period_end=AUG_END,
        farm="GAD",
    )
    assert report["farm"] == "GAD"
    assert report["farm_label"] == "Green Acre Dairy"
    assert "Coomb Milker Premix" in report["rations"]
    assert report["ingredients"][0]["ingredient_name"] == "Grass Silage"
    assert report["ingredients"][1]["ingredient_name"] == "Rape Meal"
    assert report["ingredients"][1]["as_fed_kg"] == 522_593
    assert report["totals"]["as_fed_kg"] == 6_920_631
    assert report["totals"]["dm_kg"] == 3_874_860
    assert report["totals"]["cost"] == 1_022_640.75
    assert report["days"] == 31
    assert report["averages"]["as_fed_kg"] == round(6_920_631 / 31, 2)
    assert report["ingredients"][0]["as_fed_mt_per_day"] == round((6_398_038 / 1000) / 31, 4)
    assert report["totals"]["as_fed_mt_per_day"] == round((6_920_631 / 1000) / 31, 4)
    assert report["source"] == "Loaded Mixes → By Ingredient"


def test_import_replaces_only_selected_month(db: Session) -> None:
    july_start, july_end = month_bounds(2026, 7)
    db.add(
        FeedUsageRecord(
            period_start=july_start,
            period_end=july_end,
            farm="CM",
            ingredient_name="Keep Me",
            as_fed_kg=1,
            dm_kg=1,
            cost=1,
        )
    )
    db.commit()

    payload_rows = [
        {
            "farm": "GAD",
            "ingredient_name": "Rape Meal",
            "as_fed_kg": 522593,
            "dm_kg": 470000,
            "cost": 150000,
        },
        {
            "farm": "CM",
            "ingredient_name": "Rape Meal",
            "as_fed_kg": 1000,
            "dm_kg": 900,
            "cost": 250,
        },
    ]
    with patch(
        "app.services.feed_usage.fetch_loaded_mix_ingredient_usage",
        return_value=payload_rows,
    ):
        import_feed_usage(db, month=AUG_START)

    names = {
        (row.period_start, row.farm, row.ingredient_name)
        for row in db.scalars(select(FeedUsageRecord)).all()
    }
    assert (july_start, "CM", "Keep Me") in names
    assert (AUG_START, "GAD", "Rape Meal") in names
    assert (AUG_START, "CM", "Rape Meal") in names
    august = get_usage_report(db, month=AUG_START, farm="GAD")
    assert august["ingredients"][0]["as_fed_kg"] == 522593
    assert august["row_count"] == 1
    assert august["farm"] == "GAD"
    cm = get_usage_report(db, month=AUG_START, farm="CM")
    assert cm["ingredients"][0]["as_fed_kg"] == 1000
    assert cm["row_count"] == 1


def test_partial_month_refresh_is_dropped_after_the_import_day(db: Session) -> None:
    october_start, october_end = month_bounds(2026, 10)
    september_start, september_end = month_bounds(2026, 9)
    db.add_all(
        [
            FeedUsageRecord(
                period_start=october_start,
                period_end=october_end,
                farm="GAD",
                ingredient_name="Rape Meal",
                as_fed_kg=50,
                dm_kg=40,
                cost=5,
                import_timestamp=dt.datetime(2026, 10, 1, 9, 0),
            ),
            FeedUsageRecord(
                period_start=september_start,
                period_end=september_end,
                farm="GAD",
                ingredient_name="Grass Silage",
                as_fed_kg=200,
                dm_kg=60,
                cost=2,
                import_timestamp=dt.datetime(2026, 10, 1, 9, 0),
            ),
        ]
    )
    db.commit()

    later = get_usage_report(db, month=october_start, farm="GAD", today=dt.date(2026, 10, 3))
    assert later["ingredients"] == []
    assert "left out" in later["partial_notice"]
    remaining = list(db.scalars(select(FeedUsageRecord)).all())
    assert [row.period_start for row in remaining] == [september_start]

    db.add(
        FeedUsageRecord(
            period_start=october_start,
            period_end=october_end,
            farm="GAD",
            ingredient_name="Rape Meal",
            as_fed_kg=80,
            dm_kg=70,
            cost=8,
            import_timestamp=dt.datetime(2026, 10, 3, 11, 0),
        )
    )
    db.commit()
    same_day = get_usage_report(db, month=october_start, farm="GAD", today=dt.date(2026, 10, 3))
    assert same_day["ingredients"][0]["as_fed_kg"] == 80
    assert same_day["partial_notice"] == (
        "October 2026 is still in progress. This refresh is kept for today only."
    )


def test_import_range_refreshes_each_selected_month(db: Session) -> None:
    september = dt.date(2026, 9, 1)
    october = dt.date(2026, 10, 1)

    def fake_fetch(_db, *, period_start, period_end):
        if period_start == september:
            return []
        return [
            {
                "farm": "GAD",
                "ingredient_name": "Rape Meal",
                "as_fed_kg": period_start.month * 10,
                "dm_kg": 1,
                "cost": 1,
            }
        ]

    with patch(
        "app.services.feed_usage.fetch_loaded_mix_ingredient_usage",
        side_effect=fake_fetch,
    ) as fetch:
        result = import_feed_usage_months(db, months=[AUG_START, september, october])

    assert fetch.call_count == 3
    assert result["months"] == ["2026-08", "2026-10"]
    assert result["skipped"] == ["2026-09"]
    stored = {
        row.period_start: row.as_fed_kg
        for row in db.scalars(select(FeedUsageRecord)).all()
    }
    assert stored == {AUG_START: 80, october: 100}
    assert get_import_status()["status"] == "complete"
    assert "2026-08 to 2026-10" in get_import_status()["message"]
    assert "2026-09" in get_import_status()["message"]


def test_fetch_uses_loaded_mixes_endpoint_not_fed_mixes() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [
        {
            "ingredientName": "Rape Meal",
            "quantity": 999999,
            "drymatterQuantity": 999999,
            "cost": 999999,
            "rations": [
                {
                    "rationName": "Coomb Milker Premix",
                    "quantity": 522593,
                    "drymatterQuantity": 470000,
                    "cost": 150000,
                }
            ],
        }
    ]
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    with (
        patch("app.services.feedlync_api.httpx.Client", return_value=client),
        patch(
            "app.services.feedlync_api._authenticate_farms",
            return_value=("token", ["farm-1"]),
        ),
        patch(
            "app.services.feed_usage_settings.ration_farm_lookup",
            return_value={"coomb milker premix": "GAD"},
        ),
        patch(
            "app.services.feed_usage_settings.ingredient_inclusion_lookup",
            return_value=None,
        ),
    ):
        rows = fetch_loaded_mix_ingredient_usage(
            MagicMock(), period_start=AUG_START, period_end=AUG_END
        )

    assert rows[0]["ingredient_name"] == "Rape Meal"
    assert rows[0]["farm"] == "GAD"
    assert rows[0]["as_fed_kg"] == 522593
    urls = [call.args[0] for call in client.get.call_args_list]
    spend_calls = [
        call for call in client.get.call_args_list if _LOADED_MIX_INGREDIENT_PATH in call.args[0]
    ]
    assert spend_calls
    assert not any("fedmixes" in url.lower() for url in urls)
    params = spend_calls[0].kwargs["params"]
    assert params["from"] == "2026-08-01T00:00:00.000Z"
    assert params["to"] == "2026-09-01T00:00:00.000Z"


def test_farm_for_usage_ration_matches_named_rations() -> None:
    assert farm_for_usage_ration("Coomb Milker Premix") == "GAD"
    assert farm_for_usage_ration("coomb far off") == "GAD"
    assert farm_for_usage_ration("Cwrt Milkers") == "CM"
    assert farm_for_usage_ration("Coomb Milkers") is None
    assert farm_for_usage_ration("Cwrt Pre-Bullers") is None


def test_parse_ingredient_spends_by_farm_ignores_parent_totals() -> None:
    rows = _parse_ingredient_spends_by_farm(
        [
            {
                "ingredientName": "Rape Meal",
                "quantity": 999999,
                "drymatterQuantity": 999999,
                "cost": 999999,
                "rations": [
                    {
                        "rationName": "Coomb Milker Premix",
                        "quantity": 100,
                        "drymatterQuantity": 90,
                        "cost": 10,
                    },
                    {
                        "rationName": "Coomb Milkers",
                        "quantity": 500,
                        "drymatterQuantity": 450,
                        "cost": 50,
                    },
                    {
                        "rationName": "Cwrt Milkers",
                        "quantity": 200,
                        "drymatterQuantity": 180,
                        "cost": 20,
                    },
                    {
                        "rationName": "Coomb Pregnant Heifers",
                        "quantity": 25,
                        "drymatterQuantity": 20,
                        "cost": 4,
                    },
                ],
            }
        ]
    )
    by_farm = {row["farm"]: row for row in rows}
    assert by_farm["GAD"]["as_fed_kg"] == 125
    assert by_farm["GAD"]["dm_kg"] == 110
    assert by_farm["GAD"]["cost"] == 14
    assert by_farm["CM"]["as_fed_kg"] == 200
    assert by_farm["CM"]["dm_kg"] == 180
    assert by_farm["CM"]["cost"] == 20


def test_parse_ingredient_spends_by_farm_excludes_forage_type() -> None:
    rows = _parse_ingredient_spends_by_farm(
        [
            {
                "ingredientName": "Maize Silage",
                "ingredientTypeId": 1,
                "quantity": 400000,
                "rations": [
                    {
                        "rationName": "Coomb Milker Premix",
                        "quantity": 400000,
                        "drymatterQuantity": 140000,
                        "cost": 1,
                    }
                ],
            },
            {
                "ingredientName": "Rape Meal",
                "ingredientTypeId": 2,
                "quantity": 522593,
                "rations": [
                    {
                        "rationName": "Coomb Milker Premix",
                        "quantity": 522593,
                        "drymatterQuantity": 470000,
                        "cost": 150000,
                    }
                ],
            },
            {
                "ingredientName": "Grass Silage",
                "ingredientTypeName": "Forage",
                "quantity": 1000,
                "rations": [
                    {
                        "rationName": "Coomb Milker Premix",
                        "quantity": 1000,
                        "drymatterQuantity": 300,
                        "cost": 1,
                    }
                ],
            },
        ]
    )
    assert [row["ingredient_name"] for row in rows] == ["Rape Meal"]
    assert rows[0]["as_fed_kg"] == 522593


def test_parse_uses_saved_ration_lookup_not_defaults() -> None:
    rows = _parse_ingredient_spends_by_farm(
        [
            {
                "ingredientName": "Rape Meal",
                "ingredientTypeId": 2,
                "rations": [
                    {
                        "rationName": "Coomb Milker Premix",
                        "quantity": 100,
                        "drymatterQuantity": 90,
                        "cost": 10,
                    },
                    {
                        "rationName": "Custom GAD Mix",
                        "quantity": 50,
                        "drymatterQuantity": 45,
                        "cost": 5,
                    },
                ],
            }
        ],
        ration_lookup={"custom gad mix": "GAD"},
    )
    assert len(rows) == 1
    assert rows[0]["as_fed_kg"] == 50


def test_ration_assignments_seed_and_reassign(db: Session) -> None:
    seed_ration_assignments_if_empty(db)
    assert "Coomb Milker Premix" in assigned_ration_names(db, "GAD")
    assert ration_farm_lookup(db)["cwrt milkers"] == "CM"

    save_ration_assignment(db, ration_name="Coomb Milker Premix", farm="")
    save_ration_assignment(db, ration_name="Custom Mix", farm="GAD")
    gad = assigned_ration_names(db, "GAD")
    assert "Coomb Milker Premix" not in gad
    assert "Custom Mix" in gad
    assert "coomb milker premix" not in ration_farm_lookup(db)


def test_parse_respects_saved_ingredient_inclusion() -> None:
    payload = [
        {
            "ingredientName": "Maize Silage",
            "ingredientTypeId": 1,
            "rations": [
                {
                    "rationName": "Coomb Milker Premix",
                    "quantity": 400,
                    "drymatterQuantity": 140,
                    "cost": 1,
                }
            ],
        },
        {
            "ingredientName": "Rape Meal",
            "ingredientTypeId": 2,
            "rations": [
                {
                    "rationName": "Coomb Milker Premix",
                    "quantity": 100,
                    "drymatterQuantity": 90,
                    "cost": 10,
                }
            ],
        },
    ]
    included = _parse_ingredient_spends_by_farm(
        payload,
        ingredient_inclusion={"maize silage": True, "rape meal": False},
    )
    assert [row["ingredient_name"] for row in included] == ["Maize Silage"]
    defaulted = _parse_ingredient_spends_by_farm(payload)
    assert [row["ingredient_name"] for row in defaulted] == ["Rape Meal"]


def test_parse_adds_hand_added_from_feedplan_spends() -> None:
    payload = [
        {
            "ingredientName": "Maize Silage",
            "ingredientTypeId": 1,
            "rations": [
                {
                    "rationName": "Cwrt Close Up Ration",
                    "quantity": 18000,
                    "drymatterQuantity": 6300,
                    "cost": 1,
                }
            ],
        },
        {
            "ingredientName": "Wheat",
            "ingredientTypeId": 2,
            "rations": [
                {
                    "rationName": "Cwrt Close Up Ration",
                    "quantity": 1500,
                    "drymatterQuantity": 1300,
                    "cost": 10,
                }
            ],
        },
    ]
    feedplan = {
        "2026-08-01": [
            {
                "ingredientName": "Wheat",
                "rationName": "Cwrt Close Up Ration",
                "quantity": 1500,
            },
            {
                "ingredientName": "Ammonium Chloride",
                "rationName": "Cwrt Close Up Ration",
                "quantity": 24,
            },
        ],
        "2026-08-02": [
            {
                "ingredientName": "Ammonium Chloride",
                "rationName": "Cwrt Close Up Ration",
                "quantity": 26,
            },
        ],
    }
    rows = _parse_ingredient_spends_by_farm(
        payload,
        ration_lookup={"cwrt close up ration": "CM"},
        feedplan_payload=feedplan,
    )
    by_name = {row["ingredient_name"]: row for row in rows}
    assert "Maize Silage" not in by_name
    assert by_name["Wheat"]["as_fed_kg"] == 1500
    assert by_name["Ammonium Chloride"]["as_fed_kg"] == 50
    assert by_name["Ammonium Chloride"]["farm"] == "CM"

    excluded = _parse_ingredient_spends_by_farm(
        payload,
        ration_lookup={"cwrt close up ration": "CM"},
        feedplan_payload=feedplan,
        ingredient_inclusion={"ammonium chloride": False, "wheat": True},
    )
    assert [row["ingredient_name"] for row in excluded] == ["Wheat"]


def test_ingredient_inclusion_lookup_round_trip(db: Session) -> None:
    assert ingredient_inclusion_lookup(db) is None
    save_ingredient_assignment(db, ingredient_name="Maize Silage", included=False)
    save_ingredient_assignment(db, ingredient_name="Rape Meal", included=True)
    lookup = ingredient_inclusion_lookup(db)
    assert lookup is not None
    assert lookup["maize silage"] is False
    assert lookup["rape meal"] is True


def test_usage_report_hides_excluded_stored_ingredients(db: Session) -> None:
    db.add(
        FeedUsageRecord(
            period_start=AUG_START,
            period_end=AUG_END,
            farm="GAD",
            ingredient_name="Maize Silage",
            as_fed_kg=400,
            dm_kg=140,
            cost=1,
        )
    )
    db.add(
        FeedUsageRecord(
            period_start=AUG_START,
            period_end=AUG_END,
            farm="GAD",
            ingredient_name="Rape Meal",
            as_fed_kg=100,
            dm_kg=90,
            cost=10,
        )
    )
    db.commit()
    save_ingredient_assignment(db, ingredient_name="Maize Silage", included=False)
    save_ingredient_assignment(db, ingredient_name="Rape Meal", included=True)
    report = get_usage_report(db, month=AUG_START, farm="GAD")
    assert [row["ingredient_name"] for row in report["ingredients"]] == ["Rape Meal"]
    assert report["totals"]["as_fed_kg"] == 100
    assert report["row_count"] == 1


def test_combined_usage_report_adds_gad_and_cm(db: Session) -> None:
    db.add_all(
        [
            FeedUsageRecord(
                period_start=AUG_START,
                period_end=AUG_END,
                farm="GAD",
                ingredient_name="Rape Meal",
                as_fed_kg=1000,
                dm_kg=900,
                cost=100,
            ),
            FeedUsageRecord(
                period_start=AUG_START,
                period_end=AUG_END,
                farm="CM",
                ingredient_name="Rape Meal",
                as_fed_kg=500,
                dm_kg=450,
                cost=50,
            ),
            FeedUsageRecord(
                period_start=AUG_START,
                period_end=AUG_END,
                farm="CM",
                ingredient_name="Grass Silage",
                as_fed_kg=200,
                dm_kg=60,
                cost=20,
            ),
        ]
    )
    db.commit()
    report = get_usage_report(db, month=AUG_START, farm="combined")
    assert report["farm"] == "COMBINED"
    assert report["farm_label"] == "Combined"
    by_name = {row["ingredient_name"]: row for row in report["ingredients"]}
    assert by_name["Rape Meal"]["as_fed_kg"] == 1500
    assert by_name["Rape Meal"]["dm_kg"] == 1350
    assert by_name["Grass Silage"]["as_fed_kg"] == 200
    assert report["totals"]["as_fed_kg"] == 1700
    assert report["totals"]["as_fed_mt_per_day"] == round((1700 / 1000) / 31, 4)
    assert by_name["Rape Meal"]["as_fed_mt_per_day"] == round((1500 / 1000) / 31, 4)


def test_usage_xlsx_has_borders_and_fitted_columns() -> None:
    report = build_usage_report(
        [
            {
                "ingredient_name": "Megalac / Protected Fat",
                "as_fed_kg": 12_345,
                "dm_kg": 1,
                "cost": 1,
            }
        ],
        period_start=AUG_START,
        period_end=AUG_END,
        farm="GAD",
    )
    workbook = load_workbook(BytesIO(build_usage_xlsx(report)))
    sheet = workbook.active
    assert sheet.title == "Green Acre Dairy"
    assert [cell.value for cell in sheet[1]] == [
        "Ingredient",
        "As Fed (kg)",
        "As Fed (MT)",
        "Avg MT/day",
    ]
    assert sheet["A2"].value == "Megalac / Protected Fat"
    assert sheet["B2"].value == 12345
    assert sheet["C2"].value == 12.345
    assert sheet["D2"].value == round((12345 / 1000) / 31, 4)
    assert sheet["A3"].value == "Total"
    assert sheet["B3"].value == 12345
    assert sheet["C3"].value == 12.345
    assert sheet["D3"].value == round((12345 / 1000) / 31, 4)
    assert sheet["B2"].number_format == "#,##0"
    assert sheet["C2"].number_format == "#,##0.0000"
    assert sheet["D2"].number_format == "#,##0.0000"
    for row in sheet.iter_rows(min_row=1, max_row=3, min_col=1, max_col=4):
        for cell in row:
            assert cell.border.left.style == "thin"
            assert cell.border.right.style == "thin"
            assert cell.border.top.style == "thin"
            assert cell.border.bottom.style == "thin"
    assert sheet.column_dimensions["A"].width >= len("Megalac / Protected Fat")
    assert sheet.column_dimensions["B"].width >= len("As Fed (kg)")
    assert sheet.column_dimensions["C"].width >= len("As Fed (MT)")
    assert sheet.column_dimensions["D"].width >= len("Avg MT/day")


def test_usage_fiscal_year_options_include_the_last_two_years(db: Session) -> None:
    options = usage_fiscal_year_options(db, today=dt.date(2026, 10, 1))
    assert options[:2] == [2027, 2026]

    db.add(
        FeedUsageRecord(
            period_start=dt.date(2024, 5, 1),
            period_end=dt.date(2024, 5, 31),
            farm="GAD",
            ingredient_name="Rape Meal",
            as_fed_kg=1,
            dm_kg=1,
            cost=1,
        )
    )
    db.commit()
    with_history = usage_fiscal_year_options(db, today=dt.date(2026, 10, 1))
    assert with_history[0] == 2027
    assert 2025 in with_history
    assert 2026 in with_history


def test_resolve_usage_period_defaults_to_this_fiscal_year() -> None:
    october = resolve_usage_period(today=dt.date(2026, 10, 1))
    assert october["fiscal_year"] == 2027
    assert october["any_year"] is False
    assert october["is_range"] is False
    assert october["month_from"] == dt.date(2026, 9, 1)
    assert october["month_to"] == dt.date(2026, 9, 1)

    early_april = resolve_usage_period(today=dt.date(2026, 4, 2))
    assert early_april["fiscal_year"] == 2027
    assert early_april["month_from"] == dt.date(2026, 4, 1)


def test_resolve_usage_period_any_and_range() -> None:
    single = resolve_usage_period(fiscal_year="any", month="2025-08", today=dt.date(2026, 10, 1))
    assert single["any_year"] is True
    assert single["fiscal_year"] is None
    assert single["is_range"] is False
    assert single["month_from"] == dt.date(2025, 8, 1)

    span = resolve_usage_period(
        fiscal_year="any",
        month="range",
        month_from="2025-11",
        month_to="2026-05",
        today=dt.date(2026, 10, 1),
    )
    assert span["is_range"] is True
    assert span["months"][0] == dt.date(2025, 11, 1)
    assert span["months"][-1] == dt.date(2026, 5, 1)
    assert len(span["months"]) == 7


def test_resolve_usage_period_keeps_months_inside_fiscal_year() -> None:
    period = resolve_usage_period(
        fiscal_year="2027",
        month="range",
        month_from="2026-04",
        month_to="2027-03",
        today=dt.date(2026, 10, 1),
    )
    assert period["fiscal_year"] == 2027
    assert len(period["months"]) == 12

    with pytest.raises(ValueError, match="inside the selected fiscal year"):
        resolve_usage_period(
            fiscal_year="2027",
            month="2025-08",
            today=dt.date(2026, 10, 1),
        )

    with pytest.raises(ValueError, match="end month is before"):
        resolve_usage_period(
            fiscal_year="any",
            month="range",
            month_from="2026-06",
            month_to="2026-04",
        )


def test_usage_report_sums_a_month_range(db: Session) -> None:
    db.add_all(
        [
            FeedUsageRecord(
                period_start=AUG_START,
                period_end=AUG_END,
                farm="GAD",
                ingredient_name="Rape Meal",
                as_fed_kg=1000,
                dm_kg=900,
                cost=10,
            ),
            FeedUsageRecord(
                period_start=dt.date(2026, 9, 1),
                period_end=dt.date(2026, 9, 30),
                farm="GAD",
                ingredient_name="Rape Meal",
                as_fed_kg=500,
                dm_kg=450,
                cost=5,
            ),
            FeedUsageRecord(
                period_start=dt.date(2026, 9, 1),
                period_end=dt.date(2026, 9, 30),
                farm="GAD",
                ingredient_name="Grass Silage",
                as_fed_kg=200,
                dm_kg=60,
                cost=2,
            ),
        ]
    )
    db.commit()
    report = get_usage_report(
        db,
        month=AUG_START,
        month_to=dt.date(2026, 9, 1),
        farm="GAD",
    )
    by_name = {row["ingredient_name"]: row for row in report["ingredients"]}
    assert report["period_start"] == "2026-08-01"
    assert report["period_end"] == "2026-09-30"
    assert report["days"] == 61
    assert by_name["Rape Meal"]["as_fed_kg"] == 1500
    assert by_name["Grass Silage"]["as_fed_kg"] == 200
    assert report["totals"]["as_fed_kg"] == 1700
    assert report["totals"]["as_fed_mt_per_day"] == round((1700 / 1000) / 61, 4)

