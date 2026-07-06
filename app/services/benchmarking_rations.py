"""Ration ingredients and monthly costs for the Benchmarking section."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    RATION_INGREDIENT_CATEGORIES,
    RationIngredient,
    RationIngredientCost,
)
from app.services.benchmarking import available_fiscal_years, fiscal_year_months

RATION_CATEGORY_ORDER: tuple[str, ...] = RATION_INGREDIENT_CATEGORIES

RATION_CATEGORY_META: dict[str, dict[str, str]] = {
    "concentrate": {"label": "Concentrate", "css": "concentrate"},
    "forage": {"label": "Forage", "css": "forage"},
    "straw": {"label": "Straw", "css": "straw"},
}


def list_ingredient_categories() -> list[dict[str, str]]:
    return [
        {"id": key, **RATION_CATEGORY_META[key]}
        for key in RATION_CATEGORY_ORDER
    ]


def _ingredient_sort_key(ingredient: RationIngredient) -> tuple[int, int, str]:
    try:
        cat_idx = RATION_CATEGORY_ORDER.index(ingredient.category)
    except ValueError:
        cat_idx = len(RATION_CATEGORY_ORDER)
    return (cat_idx, ingredient.sort_order, ingredient.name.lower())


def list_ingredients(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(RationIngredient)
        .where(RationIngredient.is_active.is_(True))
        .order_by(RationIngredient.sort_order, RationIngredient.name)
    ).all()
    rows = sorted(rows, key=_ingredient_sort_key)
    return [
        {
            "id": row.id,
            "name": row.name,
            "category": row.category,
            "category_label": RATION_CATEGORY_META.get(row.category, {}).get(
                "label", row.category
            ),
            "sort_order": row.sort_order,
        }
        for row in rows
    ]


def create_ingredient(
    db: Session,
    *,
    name: str,
    category: str,
    user_id: int | None,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Ingredient name is required")
    if category not in RATION_INGREDIENT_CATEGORIES:
        raise ValueError(
            f"category must be one of {list(RATION_INGREDIENT_CATEGORIES)}"
        )
    max_order = db.scalar(
        select(func.coalesce(func.max(RationIngredient.sort_order), -1)).where(
            RationIngredient.category == category
        )
    )
    row = RationIngredient(
        name=clean_name,
        category=category,
        sort_order=int(max_order or -1) + 1,
        created_by_user_id=user_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"An ingredient named '{clean_name}' already exists") from exc
    db.refresh(row)
    created = {
        "id": row.id,
        "name": row.name,
        "category": row.category,
        "category_label": RATION_CATEGORY_META[row.category]["label"],
        "sort_order": row.sort_order,
    }
    return created


def list_ingredient_costs(db: Session, *, fiscal_year: int) -> dict[str, Any]:
    months = fiscal_year_months(fiscal_year)
    ingredients = list_ingredients(db)
    ingredient_ids = {ing["id"] for ing in ingredients}

    stored = db.scalars(
        select(RationIngredientCost).where(
            RationIngredientCost.fiscal_year == fiscal_year
        )
    ).all()
    by_key: dict[tuple[str, int], float | None] = {}
    for line in stored:
        if line.ingredient_id not in ingredient_ids:
            continue
        by_key[(line.cost_month.isoformat(), line.ingredient_id)] = line.cost

    rows = []
    for month_start in months:
        month_iso = month_start.isoformat()
        costs: dict[str, float | None] = {}
        for ing in ingredients:
            costs[str(ing["id"])] = by_key.get((month_iso, ing["id"]))
        rows.append({
            "cost_month": month_iso,
            "month_label": month_start.strftime("%b-%y"),
            "costs": costs,
        })

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "categories": list_ingredient_categories(),
        "ingredients": ingredients,
        "rows": rows,
    }


def save_ingredient_costs(
    db: Session,
    *,
    fiscal_year: int,
    rows: list[dict[str, Any]],
    user_id: int | None,
) -> dict[str, Any]:
    valid_months = {m.isoformat() for m in fiscal_year_months(fiscal_year)}
    ingredient_ids = {ing["id"] for ing in list_ingredients(db)}

    for row in rows:
        cost_month_raw = row.get("cost_month")
        ingredient_id = row.get("ingredient_id")
        if not cost_month_raw or ingredient_id not in ingredient_ids:
            continue
        if isinstance(cost_month_raw, dt.date):
            cost_month = cost_month_raw
        else:
            cost_month = dt.date.fromisoformat(str(cost_month_raw))
        if cost_month.isoformat() not in valid_months:
            continue

        cost_raw = row.get("cost")
        cost: float | None
        if cost_raw is None or cost_raw == "":
            cost = None
        else:
            cost = float(cost_raw)

        existing = db.scalar(
            select(RationIngredientCost).where(
                RationIngredientCost.fiscal_year == fiscal_year,
                RationIngredientCost.cost_month == cost_month,
                RationIngredientCost.ingredient_id == ingredient_id,
            )
        )
        if cost is None:
            if existing:
                db.delete(existing)
            continue
        if existing:
            existing.cost = cost
            existing.updated_by_user_id = user_id
        else:
            db.add(
                RationIngredientCost(
                    fiscal_year=fiscal_year,
                    cost_month=cost_month,
                    ingredient_id=ingredient_id,
                    cost=cost,
                    updated_by_user_id=user_id,
                )
            )

    db.commit()
    return list_ingredient_costs(db, fiscal_year=fiscal_year)
