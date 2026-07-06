"""Tests for Benchmarking rations (ingredients and costs)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, RationIngredient, RationIngredientCost
from app.services.benchmarking_rations import (
    create_ingredient,
    list_ingredient_categories,
    list_ingredient_costs,
    list_ingredients,
    save_ingredient_costs,
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
