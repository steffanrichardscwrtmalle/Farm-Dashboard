"""Tests for Feedlync Loaded Mixes feed usage."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, FeedUsageRecord
from app.services.feed_usage import (
    build_usage_report,
    get_usage_report,
    import_feed_usage,
    resolve_usage_month,
)
from app.services.feedlync_api import (
    _LOADED_MIX_INGREDIENT_PATH,
    _parse_ingredient_spends,
    _utc_month_range,
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
    )
    assert report["ingredients"][0]["ingredient_name"] == "Grass Silage"
    assert report["ingredients"][1]["ingredient_name"] == "Rape Meal"
    assert report["ingredients"][1]["as_fed_kg"] == 522_593
    assert report["totals"]["as_fed_kg"] == 6_920_631
    assert report["totals"]["dm_kg"] == 3_874_860
    assert report["totals"]["cost"] == 1_022_640.75
    assert report["days"] == 31
    assert report["averages"]["as_fed_kg"] == round(6_920_631 / 31, 2)
    assert report["source"] == "Loaded Mixes → By Ingredient"


def test_import_replaces_only_selected_month(db: Session) -> None:
    july_start, july_end = month_bounds(2026, 7)
    db.add(
        FeedUsageRecord(
            period_start=july_start,
            period_end=july_end,
            ingredient_name="Keep Me",
            as_fed_kg=1,
            dm_kg=1,
            cost=1,
        )
    )
    db.commit()

    payload = [
        {
            "ingredientName": "Rape Meal",
            "quantity": 522593,
            "drymatterQuantity": 470000,
            "cost": 150000,
        }
    ]
    with patch(
        "app.services.feed_usage.fetch_loaded_mix_ingredient_usage",
        return_value=_parse_ingredient_spends(payload),
    ):
        import_feed_usage(db, month=AUG_START)

    names = {
        (row.period_start, row.ingredient_name)
        for row in db.scalars(select(FeedUsageRecord)).all()
    }
    assert (july_start, "Keep Me") in names
    assert (AUG_START, "Rape Meal") in names
    august = get_usage_report(db, month=AUG_START)
    assert august["ingredients"][0]["as_fed_kg"] == 522593
    assert august["row_count"] == 1


def test_fetch_uses_loaded_mixes_endpoint_not_fed_mixes() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [
        {
            "ingredientName": "Rape Meal",
            "quantity": 522593,
            "drymatterQuantity": 470000,
            "cost": 150000,
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
    ):
        rows = fetch_loaded_mix_ingredient_usage(
            MagicMock(), period_start=AUG_START, period_end=AUG_END
        )

    assert rows[0]["ingredient_name"] == "Rape Meal"
    assert rows[0]["as_fed_kg"] == 522593
    url = client.get.call_args.args[0]
    assert _LOADED_MIX_INGREDIENT_PATH in url
    assert "fedmix" not in url.lower()
    params = client.get.call_args.kwargs["params"]
    assert params["from"] == "2026-08-01T00:00:00.000Z"
    assert params["to"] == "2026-09-01T00:00:00.000Z"
