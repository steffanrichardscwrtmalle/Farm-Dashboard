"""Tests for farm ration recipes and monthly inclusions."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, FarmRationInclusion
from app.services.benchmarking_farm_rations import (
    create_farm_ration,
    get_farm_ration_workbook,
    get_ration_cost_comparison,
    ration_base_name,
    save_farm_ration_inclusions,
    update_farm_ration,
)
from app.services.benchmarking_rations import create_ingredient, save_ingredient_costs


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _seed_ingredients(db: Session) -> tuple[dict, dict]:
    a = create_ingredient(db, name="Blend", category="concentrate", user_id=1)
    b = create_ingredient(db, name="Grass", category="forage", user_id=1)
    return a, b


def test_create_ration_and_workbook(db: Session) -> None:
    a, b = _seed_ingredients(db)
    ration = create_farm_ration(
        db,
        farm="cm",
        name="Milkers",
        ingredient_ids=[a["id"], b["id"]],
        user_id=1,
    )
    workbook = get_farm_ration_workbook(db, farm="CM", fiscal_year=2026)
    assert len(workbook["rations"]) == 1
    assert workbook["rations"][0]["id"] == ration["id"]
    assert len(workbook["rations"][0]["rows"]) == 12


def test_save_inclusions_calculates_cost_per_head(db: Session) -> None:
    a, b = _seed_ingredients(db)
    ration = create_farm_ration(
        db,
        farm="gad",
        name="Dry cows",
        ingredient_ids=[a["id"]],
        user_id=1,
    )
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[
            {
                "cost_month": "2025-04-01",
                "ingredient_id": a["id"],
                "cost": 300.0,
            }
        ],
        user_id=1,
    )
    save_farm_ration_inclusions(
        db,
        farm="gad",
        ration_id=ration["id"],
        fiscal_year=2026,
        rows=[
            {
                "inclusion_month": "2025-04-01",
                "ingredient_id": a["id"],
                "kg_per_head": 10.0,
            }
        ],
        user_id=1,
    )
    workbook = get_farm_ration_workbook(
        db, farm="gad", fiscal_year=2026, ration_id=ration["id"]
    )
    april = workbook["rations"][0]["rows"][0]
    assert april["inclusions"][str(a["id"])] == 10.0
    assert april["cost_per_head"] == 3.0


def test_blank_inclusion_uses_previous_month(db: Session) -> None:
    a, _b = _seed_ingredients(db)
    ration = create_farm_ration(
        db, farm="cm", name="Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[
            {"cost_month": "2025-04-01", "ingredient_id": a["id"], "cost": 300.0},
            {"cost_month": "2025-05-01", "ingredient_id": a["id"], "cost": 300.0},
        ],
        user_id=1,
    )
    save_farm_ration_inclusions(
        db,
        farm="cm",
        ration_id=ration["id"],
        fiscal_year=2026,
        rows=[
            {
                "inclusion_month": "2025-04-01",
                "ingredient_id": a["id"],
                "kg_per_head": 10.0,
            }
        ],
        user_id=1,
    )
    workbook = get_farm_ration_workbook(
        db, farm="cm", fiscal_year=2026, ration_id=ration["id"]
    )
    april = workbook["rations"][0]["rows"][0]
    may = workbook["rations"][0]["rows"][1]
    assert april["entered_inclusions"][str(a["id"])] == 10.0
    assert may["entered_inclusions"][str(a["id"])] is None
    assert may["inclusions"][str(a["id"])] == 10.0
    assert may["cost_per_head"] == 3.0


def test_blank_inclusion_carries_from_previous_fiscal_year(db: Session) -> None:
    a, _b = _seed_ingredients(db)
    ration = create_farm_ration(
        db, farm="gad", name="Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    save_farm_ration_inclusions(
        db,
        farm="gad",
        ration_id=ration["id"],
        fiscal_year=2026,
        rows=[
            {
                "inclusion_month": "2026-03-01",
                "ingredient_id": a["id"],
                "kg_per_head": 8.0,
            }
        ],
        user_id=1,
    )
    workbook = get_farm_ration_workbook(
        db, farm="gad", fiscal_year=2027, ration_id=ration["id"]
    )
    april = workbook["rations"][0]["rows"][0]
    assert april["inclusion_month"] == "2026-04-01"
    assert april["entered_inclusions"][str(a["id"])] is None
    assert april["inclusions"][str(a["id"])] == 8.0


def test_update_ration_changes_ingredients(db: Session) -> None:
    a, b = _seed_ingredients(db)
    ration = create_farm_ration(
        db,
        farm="cm",
        name="Heifers",
        ingredient_ids=[a["id"]],
        user_id=1,
    )
    updated = update_farm_ration(
        db,
        ration_id=ration["id"],
        farm="cm",
        name="Growing heifers",
        ingredient_ids=[b["id"]],
    )
    assert updated["name"] == "Growing heifers"
    assert updated["ingredient_ids"] == [b["id"]]
    assert db.query(FarmRationInclusion).count() == 0


def test_ration_base_name_strips_farm_prefix() -> None:
    assert ration_base_name("CM Milkers", "CM") == "Milkers"
    assert ration_base_name("GAD Milkers", "gad") == "Milkers"
    assert ration_base_name("Milkers", "CM") is None


def test_ration_cost_comparison_pairs_by_suffix(db: Session) -> None:
    a = create_ingredient(db, name="Blend", category="concentrate", user_id=1)
    cm = create_farm_ration(
        db, farm="cm", name="CM Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    gad = create_farm_ration(
        db, farm="gad", name="GAD Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[{"cost_month": "2025-04-01", "ingredient_id": a["id"], "cost": 310.0}],
        user_id=1,
    )
    save_farm_ration_inclusions(
        db,
        farm="cm",
        ration_id=cm["id"],
        fiscal_year=2026,
        rows=[{
            "inclusion_month": "2025-04-01",
            "ingredient_id": a["id"],
            "kg_per_head": 10.0,
        }],
        user_id=1,
    )
    save_farm_ration_inclusions(
        db,
        farm="gad",
        ration_id=gad["id"],
        fiscal_year=2026,
        rows=[{
            "inclusion_month": "2025-04-01",
            "ingredient_id": a["id"],
            "kg_per_head": 12.0,
        }],
        user_id=1,
    )
    result = get_ration_cost_comparison(db, fiscal_year=2026)
    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["base_name"] == "Milkers"
    april = result["comparisons"][0]["rows"][0]
    assert april["cm"]["cost_per_head_day"] == 3.1
    assert april["gad"]["cost_per_head_day"] == 3.72


def test_workbook_any_year_includes_months_across_fiscal_years(db: Session) -> None:
    a, _b = _seed_ingredients(db)
    ration = create_farm_ration(
        db, farm="cm", name="Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    save_farm_ration_inclusions(
        db,
        farm="cm",
        ration_id=ration["id"],
        fiscal_year=None,
        month_from=dt.date(2025, 4, 1),
        month_to=dt.date(2027, 3, 1),
        rows=[
            {
                "inclusion_month": "2025-04-01",
                "ingredient_id": a["id"],
                "kg_per_head": 8.0,
            },
            {
                "inclusion_month": "2026-04-01",
                "ingredient_id": a["id"],
                "kg_per_head": 9.0,
            },
        ],
        user_id=1,
    )
    workbook = get_farm_ration_workbook(
        db,
        farm="cm",
        fiscal_year=None,
        month_from=dt.date(2025, 4, 1),
        month_to=dt.date(2027, 3, 1),
    )
    assert workbook["any_year"] is True
    assert len(workbook["rations"][0]["rows"]) == 24
    by_month = {
        row["inclusion_month"]: row for row in workbook["rations"][0]["rows"]
    }
    assert by_month["2025-04-01"]["inclusions"][str(a["id"])] == 8.0
    assert by_month["2026-04-01"]["inclusions"][str(a["id"])] == 9.0
    stored = db.query(FarmRationInclusion).all()
    fy_by_month = {row.inclusion_month.isoformat(): row.fiscal_year for row in stored}
    assert fy_by_month["2025-04-01"] == 2026
    assert fy_by_month["2026-04-01"] == 2027


def test_cost_comparison_any_year_uses_custom_range(db: Session) -> None:
    a = create_ingredient(db, name="Blend", category="concentrate", user_id=1)
    create_farm_ration(
        db, farm="cm", name="CM Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    create_farm_ration(
        db, farm="gad", name="GAD Milkers", ingredient_ids=[a["id"]], user_id=1
    )
    result = get_ration_cost_comparison(
        db,
        fiscal_year=None,
        month_from=dt.date(2025, 10, 1),
        month_to=dt.date(2026, 6, 1),
    )
    assert result["any_year"] is True
    assert len(result["comparisons"][0]["rows"]) == 9
    assert result["comparisons"][0]["rows"][0]["inclusion_month"] == "2025-10-01"
    assert result["comparisons"][0]["rows"][-1]["inclusion_month"] == "2026-06-01"
