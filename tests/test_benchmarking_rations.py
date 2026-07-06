"""Tests for Benchmarking rations (ingredients and costs)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, RationIngredient, RationIngredientCost
from app.services.benchmarking_rations import (
    create_ingredient,
    deactivate_ingredient,
    list_ingredient_categories,
    list_ingredient_costs,
    list_ingredients,
    save_ingredient_costs,
    update_ingredient,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_list_ingredient_categories() -> None:
    cats = list_ingredient_categories()
    assert [c["id"] for c in cats] == ["concentrate", "forage", "straw"]


def test_create_ingredient_groups_by_category(db: Session) -> None:
    create_ingredient(db, name="Straw", category="straw", user_id=1)
    create_ingredient(db, name="Maize", category="forage", user_id=1)
    create_ingredient(db, name="Blend A", category="concentrate", user_id=1)
    create_ingredient(db, name="Blend B", category="concentrate", user_id=1)

    ingredients = list_ingredients(db)
    assert [i["name"] for i in ingredients] == [
        "Blend A",
        "Blend B",
        "Maize",
        "Straw",
    ]
    assert [i["category"] for i in ingredients] == [
        "concentrate",
        "concentrate",
        "forage",
        "straw",
    ]


def test_list_ingredient_costs_empty_grid(db: Session) -> None:
    create_ingredient(db, name="Wheat", category="concentrate", user_id=None)
    result = list_ingredient_costs(db, fiscal_year=2026)
    assert result["fiscal_year"] == 2026
    assert len(result["rows"]) == 12
    assert result["rows"][0]["month_label"] == "Apr-25"
    assert result["rows"][0]["costs"]


def test_save_and_reload_ingredient_costs(db: Session) -> None:
    ing = create_ingredient(db, name="Barley", category="concentrate", user_id=1)
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[
            {
                "cost_month": "2025-04-01",
                "ingredient_id": ing["id"],
                "cost": 245.5,
            }
        ],
        user_id=1,
    )
    result = list_ingredient_costs(db, fiscal_year=2026)
    april = result["rows"][0]
    assert april["cost_month"] == "2025-04-01"
    assert april["costs"][str(ing["id"])] == 245.5
    assert db.query(RationIngredientCost).count() == 1


def test_create_ingredient_rejects_duplicate_name(db: Session) -> None:
    create_ingredient(db, name="Soya", category="concentrate", user_id=1)
    with pytest.raises(ValueError, match="already exists"):
        create_ingredient(db, name="Soya", category="forage", user_id=1)


def test_save_ingredient_costs_clears_blank(db: Session) -> None:
    ing = create_ingredient(db, name="Grass", category="forage", user_id=1)
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[
            {
                "cost_month": dt.date(2025, 5, 1),
                "ingredient_id": ing["id"],
                "cost": 90.0,
            }
        ],
        user_id=1,
    )
    assert db.query(RationIngredientCost).count() == 1
    save_ingredient_costs(
        db,
        fiscal_year=2026,
        rows=[
            {
                "cost_month": dt.date(2025, 5, 1),
                "ingredient_id": ing["id"],
                "cost": None,
            }
        ],
        user_id=1,
    )
    assert db.query(RationIngredientCost).count() == 0


def test_update_ingredient_name_and_category(db: Session) -> None:
    ing = create_ingredient(db, name="Blend", category="concentrate", user_id=1)
    updated = update_ingredient(
        db,
        ingredient_id=ing["id"],
        name="High Energy Blend",
        category="forage",
    )
    assert updated["name"] == "High Energy Blend"
    assert updated["category"] == "forage"
    ingredients = list_ingredients(db)
    assert len(ingredients) == 1
    assert ingredients[0]["name"] == "High Energy Blend"


def test_deactivate_ingredient_hides_from_library(db: Session) -> None:
    ing = create_ingredient(db, name="Old Feed", category="straw", user_id=1)
    deactivate_ingredient(db, ingredient_id=ing["id"])
    assert list_ingredients(db) == []
    row = db.get(RationIngredient, ing["id"])
    assert row is not None
    assert row.is_active is False
