"""Tests for feed purchase forecasts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    BenchmarkForecastLine,
    HerdInventory,
    StockOpeningBaseline,
)
from app.services.benchmarking_farm_rations import (
    category_cost_per_head_day,
    create_farm_ration,
    ration_suffix_key,
    save_farm_ration_inclusions,
)
from app.services.benchmarking_rations import create_ingredient, save_ingredient_costs
from app.services.feed_purchase_forecasts import (
    _compute_month_category,
    build_feed_purchase_forecasts_report,
)

TODAY = dt.date(2026, 7, 6)
FISCAL_YEAR = 2027


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_ration_suffix_key_matches_expected_names() -> None:
    assert ration_suffix_key("Milkers") == "milkers"
    assert ration_suffix_key("Far Off") == "far_off"
    assert ration_suffix_key("Close Up") == "close_up"
    assert ration_suffix_key("Bullers") == "bullers"
    assert ration_suffix_key("Bulling Heifer") == "bullers"
    assert ration_suffix_key("Unknown") is None


def test_category_cost_per_head_day_sums_one_category_only() -> None:
    ration = {
        "ingredients": [
            {"id": 1, "category": "concentrate"},
            {"id": 2, "category": "forage"},
        ]
    }
    row = {
        "inclusions": {"1": "10", "2": "20"},
        "ingredient_costs": {"1": 300.0, "2": 100.0},
    }
    assert category_cost_per_head_day(row, ration, "concentrate") == 3.0
    assert category_cost_per_head_day(row, ration, "forage") == 2.0


def test_compute_month_category_dairy_concentrate_formula() -> None:
    heads = {
        "CM": {
            "2026-07-01": {
                "Dairy": {"opening": 100, "closing": 120},
                "Youngstock": {"opening": 50, "closing": 60},
                "Beef": {"opening": 10, "closing": 12},
            }
        }
    }
    ration_costs = {
        "milkers": {"2026-07-01": {"concentrate": 2.0, "forage": None, "straw": None}},
        "far_off": {"2026-07-01": {"concentrate": 1.0, "forage": None, "straw": None}},
        "close_up": {"2026-07-01": {"concentrate": 1.5, "forage": None, "straw": None}},
        "bullers": {"2026-07-01": {"concentrate": 0.5, "forage": None, "straw": None}},
    }
    result = _compute_month_category(
        farm="CM",
        month_start=dt.date(2026, 7, 1),
        category="concentrate",
        heads=heads,
        ration_costs=ration_costs,
        dry_pct=20.0,
    )
    days = 31
    avg_cows = 110.0
    avg_yb = (50 + 10 + 60 + 12) / 2.0
    dry = 0.2

    expected_milkers = avg_cows * (1 - dry) * 2.0 * days
    expected_far_off = avg_cows * dry * 0.5 * 1.0 * days
    expected_close_up = avg_cows * dry * 0.5 * 1.5 * days
    expected_calf = avg_yb * 0.125 * 2.0 * days
    expected_pre_bullers = avg_yb * 0.3333 * 2.0 * days
    expected_bullers = avg_yb * 0.15 * 0.5 * days
    expected_pregnant = avg_yb * 0.15 * 0.75 * 1.0 * days

    assert result["detail"]["milkers"] == round(expected_milkers)
    assert result["detail"]["far_off"] == round(expected_far_off)
    assert result["detail"]["close_up"] == round(expected_close_up)
    assert result["dairy"] == round(expected_milkers + expected_far_off + expected_close_up)
    assert result["youngstock"] == round(
        expected_calf + expected_pre_bullers + expected_bullers + expected_pregnant
    )


def test_compute_month_category_rollup_sums_available_components() -> None:
    heads = {
        "CM": {
            "2026-04-01": {
                "Dairy": {"opening": 100, "closing": 100},
                "Youngstock": {"opening": 0, "closing": 0},
                "Beef": {"opening": 0, "closing": 0},
            }
        }
    }
    ration_costs = {
        "milkers": {"2026-04-01": {"concentrate": 2.0, "forage": None, "straw": None}},
        "far_off": {"2026-04-01": {"concentrate": None, "forage": None, "straw": None}},
        "close_up": {"2026-04-01": {"concentrate": 1.0, "forage": None, "straw": None}},
        "bullers": {"2026-04-01": {"concentrate": 0.5, "forage": None, "straw": None}},
    }
    result = _compute_month_category(
        farm="CM",
        month_start=dt.date(2026, 4, 1),
        category="concentrate",
        heads=heads,
        ration_costs=ration_costs,
        dry_pct=10.0,
    )
    assert result["detail"]["milkers"] is not None
    assert result["detail"]["far_off"] is None
    assert result["detail"]["close_up"] is not None
    assert result["dairy"] == result["detail"]["milkers"] + result["detail"]["close_up"]


def test_compute_month_category_forage_rollup_sums_available_rations() -> None:
    heads = {
        "CM": {
            "2026-04-01": {
                "Dairy": {"opening": 100, "closing": 100},
                "Youngstock": {"opening": 40, "closing": 40},
                "Beef": {"opening": 0, "closing": 0},
            }
        }
    }
    ration_costs = {
        "milkers": {"2026-04-01": {"concentrate": None, "forage": 1.0, "straw": None}},
        "far_off": {"2026-04-01": {"concentrate": None, "forage": None, "straw": None}},
        "close_up": {"2026-04-01": {"concentrate": None, "forage": None, "straw": None}},
        "bullers": {"2026-04-01": {"concentrate": None, "forage": None, "straw": None}},
    }
    result = _compute_month_category(
        farm="CM",
        month_start=dt.date(2026, 4, 1),
        category="forage",
        heads=heads,
        ration_costs=ration_costs,
        dry_pct=10.0,
    )
    days = 30
    avg_cows = 100.0
    avg_yb = 40.0
    dry = 0.1
    expected_milkers = round(avg_cows * (1 - dry) * 1.0 * days)
    expected_calf = round(avg_yb * 0.125 * 1.0 * days)
    expected_pre_bullers = round(avg_yb * 0.3333 * 1.0 * days)

    assert result["detail"]["milkers"] == expected_milkers
    assert result["detail"]["far_off"] is None
    assert result["dairy"] == expected_milkers
    assert result["youngstock"] == expected_calf + expected_pre_bullers


def _seed_farm_baselines(db: Session) -> None:
    for farm, group, opening in [
        ("CM", "cows", 100),
        ("CM", "youngstock", 80),
        ("CM", "beef", 10),
        ("GAD", "cows", 90),
        ("GAD", "youngstock", 70),
        ("GAD", "beef", 8),
    ]:
        db.add(
            StockOpeningBaseline(
                farm=farm,
                stock_group=group,
                month_start=dt.date(2024, 4, 1),
                opening_count=opening,
            )
        )
    db.add(
        HerdInventory(
            farm="CM",
            cow_id="1",
            etag="UK1",
            bdat=dt.date(2020, 1, 1),
            lact=2,
            import_timestamp=dt.datetime(2026, 6, 30, 12, 0, 0),
        )
    )
    db.commit()


def _seed_cm_ration(
    db: Session,
    *,
    name: str,
    concentrate_kg: float = 10.0,
) -> None:
    concentrate = create_ingredient(db, name=f"{name} Blend", category="concentrate", user_id=1)
    ration = create_farm_ration(
        db,
        farm="cm",
        name=name,
        ingredient_ids=[concentrate["id"]],
        user_id=1,
    )
    save_ingredient_costs(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "cost_month": "2026-07-01",
                "ingredient_id": concentrate["id"],
                "cost": 300.0,
            },
        ],
        user_id=1,
    )
    save_farm_ration_inclusions(
        db,
        farm="cm",
        ration_id=ration["id"],
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "inclusion_month": "2026-07-01",
                "ingredient_id": concentrate["id"],
                "kg_per_head": concentrate_kg,
            },
        ],
        user_id=1,
    )


def _seed_cm_milkers_ration(db: Session) -> None:
    _seed_cm_ration(db, name="CM Milkers")
    _seed_cm_ration(db, name="CM Far Off")
    _seed_cm_ration(db, name="CM Close Up")
    _seed_cm_ration(db, name="CM Bullers")


def test_build_feed_purchase_forecasts_report_structure(db: Session) -> None:
    _seed_farm_baselines(db)
    _seed_cm_milkers_ration(db)
    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 7, 1),
            metric="dry_cows_pct",
            farm="CM",
            quantity=10.0,
        )
    )
    db.commit()

    report = build_feed_purchase_forecasts_report(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert report["fiscal_year"] == FISCAL_YEAR
    assert "CM" in report["farms"]
    assert "GAD" in report["farms"]
    cm_conc = report["farms"]["CM"]["tables"]["concentrate"]["rows"]
    assert len(cm_conc) == 12
    july = next(row for row in cm_conc if row["month_start"] == "2026-07-01")
    assert july["source"] == "projected"
    assert july["dairy"] is not None
    assert july["dairy"] > 0
    assert july["detail"]["milkers"] is not None


def test_forage_and_straw_rows_include_total(db: Session) -> None:
    _seed_farm_baselines(db)
    _seed_cm_milkers_ration(db)
    db.commit()

    report = build_feed_purchase_forecasts_report(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    july_straw = next(
        row
        for row in report["farms"]["CM"]["tables"]["straw"]["rows"]
        if row["month_start"] == "2026-07-01"
    )
    assert "total" in july_straw
    present = [
        value
        for value in (july_straw["dairy"], july_straw["youngstock"])
        if value is not None
    ]
    if present:
        assert july_straw["total"] == sum(present)
