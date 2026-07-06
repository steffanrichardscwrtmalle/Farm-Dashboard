"""Tests for farm ration recipes and monthly inclusions."""

from __future__ import annotations

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
    assert april["cm"]["cost_per_head_day"] == round(3.1 / 30, 4)
    assert april["gad"]["cost_per_head_day"] == round(3.72 / 30, 4)
