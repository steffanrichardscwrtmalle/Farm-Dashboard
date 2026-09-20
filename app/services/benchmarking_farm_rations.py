"""Farm ration recipes and monthly kg/head/day inclusions."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    HERD_FARM_OPTIONS,
    FarmRation,
    FarmRationInclusion,
    FarmRationIngredient,
    RationIngredientCost,
)
from app.services.benchmarking import ration_month_range, ration_period_meta
from app.services.benchmarking_rations import list_ingredients
from app.services.events_common import _fiscal_year_from_date, _month_start

FARM_RATION_SLUGS: dict[str, str] = {
    "cm": "CM",
    "gad": "GAD",
}

FEED_INGREDIENT_CATEGORIES: tuple[str, ...] = ("concentrate", "forage", "straw")

RATION_SUFFIX_PATTERNS: dict[str, tuple[str, ...]] = {
    "milkers": ("milkers", "milker"),
    "far_off": ("far off", "faroff"),
    "close_up": ("close up", "closeup"),
    "bullers": ("bullers", "bulling heifer", "bulling"),
}


def normalize_farm_code(farm: str) -> str:
    key = farm.strip().lower()
    if key in FARM_RATION_SLUGS:
        return FARM_RATION_SLUGS[key]
    upper = farm.strip().upper()
    if upper in HERD_FARM_OPTIONS:
        return upper
    raise ValueError(f"farm must be one of {list(FARM_RATION_SLUGS)}")


def _cost_per_head(kg_per_head: float | None, cost_per_tonne: float | None) -> float | None:
    if kg_per_head is None or cost_per_tonne is None:
        return None
    return (kg_per_head / 1000.0) * cost_per_tonne


def _latest_inclusions_before(
    db: Session, *, ration_ids: list[int], before: dt.date
) -> dict[tuple[int, int], float]:
    """Last entered kg/head for each ration/ingredient before a month."""
    if not ration_ids:
        return {}
    rows = db.scalars(
        select(FarmRationInclusion)
        .where(
            FarmRationInclusion.ration_id.in_(ration_ids),
            FarmRationInclusion.inclusion_month < before,
            FarmRationInclusion.kg_per_head.isnot(None),
        )
        .order_by(
            FarmRationInclusion.ration_id,
            FarmRationInclusion.ingredient_id,
            FarmRationInclusion.inclusion_month.desc(),
        )
    ).all()
    latest: dict[tuple[int, int], float] = {}
    for line in rows:
        key = (line.ration_id, line.ingredient_id)
        if key not in latest:
            latest[key] = line.kg_per_head
    return latest


def _total_cost_per_head(
    inclusions: dict[str, float | None],
    costs: dict[str, float | None],
    ingredient_ids: list[int],
) -> float | None:
    total = 0.0
    has_value = False
    for ingredient_id in ingredient_ids:
        key = str(ingredient_id)
        kg = inclusions.get(key)
        if kg is None:
            continue
        cost = costs.get(key)
        if cost is None:
            return None
        total += (kg / 1000.0) * cost
        has_value = True
    return round(total, 4) if has_value else None


def _ingredient_costs_by_month(
    db: Session,
    *,
    fiscal_year: int | None,
    months: list[dt.date],
    ingredient_ids: set[int],
) -> dict[str, dict[str, float | None]]:
    if not ingredient_ids or not months:
        return {}
    query = select(RationIngredientCost).where(
        RationIngredientCost.ingredient_id.in_(ingredient_ids),
        RationIngredientCost.cost_month >= months[0],
        RationIngredientCost.cost_month <= months[-1],
    )
    if fiscal_year is not None:
        query = query.where(RationIngredientCost.fiscal_year == fiscal_year)
    stored = db.scalars(query).all()
    by_month: dict[str, dict[str, float | None]] = {}
    for line in stored:
        month_key = _month_start(line.cost_month).isoformat()
        by_month.setdefault(month_key, {})[str(line.ingredient_id)] = line.cost
    return by_month


def _ration_ingredient_ids(db: Session, ration_id: int) -> list[int]:
    rows = db.scalars(
        select(FarmRationIngredient)
        .where(FarmRationIngredient.ration_id == ration_id)
        .order_by(FarmRationIngredient.sort_order, FarmRationIngredient.ingredient_id)
    ).all()
    return [row.ingredient_id for row in rows]


def _sync_ration_ingredients(
    db: Session, *, ration_id: int, ingredient_ids: list[int]
) -> list[int]:
    available = {ing["id"] for ing in list_ingredients(db)}
    ordered = [ing_id for ing_id in ingredient_ids if ing_id in available]
    if not ordered:
        raise ValueError("Select at least one ingredient")

    existing = db.scalars(
        select(FarmRationIngredient).where(
            FarmRationIngredient.ration_id == ration_id
        )
    ).all()
    existing_by_ing = {row.ingredient_id: row for row in existing}
    keep = set(ordered)

    for row in existing:
        if row.ingredient_id not in keep:
            db.delete(row)
            db.execute(
                delete(FarmRationInclusion).where(
                    FarmRationInclusion.ration_id == ration_id,
                    FarmRationInclusion.ingredient_id == row.ingredient_id,
                )
            )

    for sort_order, ingredient_id in enumerate(ordered):
        link = existing_by_ing.get(ingredient_id)
        if link:
            link.sort_order = sort_order
        else:
            db.add(
                FarmRationIngredient(
                    ration_id=ration_id,
                    ingredient_id=ingredient_id,
                    sort_order=sort_order,
                )
            )
    return ordered


def _get_active_ration(db: Session, *, ration_id: int, farm: str) -> FarmRation:
    row = db.get(FarmRation, ration_id)
    if row is None or not row.is_active or row.farm != farm:
        raise ValueError("Ration not found")
    return row


def list_farm_rations(db: Session, *, farm: str) -> list[dict[str, Any]]:
    farm_code = normalize_farm_code(farm)
    rows = db.scalars(
        select(FarmRation)
        .where(FarmRation.farm == farm_code, FarmRation.is_active.is_(True))
        .order_by(FarmRation.sort_order, FarmRation.name)
    ).all()
    all_ingredients = {ing["id"]: ing for ing in list_ingredients(db)}
    result = []
    for row in rows:
        ingredient_ids = _ration_ingredient_ids(db, row.id)
        ingredients = [
            all_ingredients[ing_id]
            for ing_id in ingredient_ids
            if ing_id in all_ingredients
        ]
        result.append({
            "id": row.id,
            "name": row.name,
            "ingredient_ids": ingredient_ids,
            "ingredients": ingredients,
        })
    return result


def create_farm_ration(
    db: Session,
    *,
    farm: str,
    name: str,
    ingredient_ids: list[int],
    user_id: int | None,
) -> dict[str, Any]:
    farm_code = normalize_farm_code(farm)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Ration name is required")
    max_order = db.scalar(
        select(func.coalesce(func.max(FarmRation.sort_order), -1)).where(
            FarmRation.farm == farm_code
        )
    )
    row = FarmRation(
        farm=farm_code,
        name=clean_name,
        sort_order=int(max_order or -1) + 1,
        created_by_user_id=user_id,
    )
    db.add(row)
    try:
        db.flush()
        ordered_ids = _sync_ration_ingredients(
            db, ration_id=row.id, ingredient_ids=ingredient_ids
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A ration named '{clean_name}' already exists") from exc
    db.refresh(row)
    all_ingredients = {ing["id"]: ing for ing in list_ingredients(db)}
    return {
        "id": row.id,
        "name": row.name,
        "ingredient_ids": ordered_ids,
        "ingredients": [all_ingredients[i] for i in ordered_ids if i in all_ingredients],
    }


def update_farm_ration(
    db: Session,
    *,
    ration_id: int,
    farm: str,
    name: str,
    ingredient_ids: list[int],
) -> dict[str, Any]:
    farm_code = normalize_farm_code(farm)
    row = _get_active_ration(db, ration_id=ration_id, farm=farm_code)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Ration name is required")
    row.name = clean_name
    ordered_ids = _sync_ration_ingredients(
        db, ration_id=ration_id, ingredient_ids=ingredient_ids
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A ration named '{clean_name}' already exists") from exc
    db.refresh(row)
    all_ingredients = {ing["id"]: ing for ing in list_ingredients(db)}
    return {
        "id": row.id,
        "name": row.name,
        "ingredient_ids": ordered_ids,
        "ingredients": [all_ingredients[i] for i in ordered_ids if i in all_ingredients],
    }


def deactivate_farm_ration(db: Session, *, ration_id: int, farm: str) -> None:
    farm_code = normalize_farm_code(farm)
    row = _get_active_ration(db, ration_id=ration_id, farm=farm_code)
    row.is_active = False
    db.commit()


def get_farm_ration_workbook(
    db: Session,
    *,
    farm: str,
    fiscal_year: int | None,
    ration_id: int | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    farm_code = normalize_farm_code(farm)
    rations = list_farm_rations(db, farm=farm_code)
    available_ingredients = list_ingredients(db)
    all_ingredient_ids = {ing["id"] for ing in available_ingredients}
    months = ration_month_range(
        fiscal_year=fiscal_year, month_from=month_from, month_to=month_to
    )

    costs_by_month = _ingredient_costs_by_month(
        db,
        fiscal_year=fiscal_year,
        months=months,
        ingredient_ids=all_ingredient_ids,
    )

    inclusion_query = select(FarmRationInclusion).where(
        FarmRationInclusion.ration_id.in_([r["id"] for r in rations] or [-1]),
    )
    if fiscal_year is not None:
        inclusion_query = inclusion_query.where(
            FarmRationInclusion.fiscal_year == fiscal_year
        )
    elif months:
        inclusion_query = inclusion_query.where(
            FarmRationInclusion.inclusion_month >= months[0],
            FarmRationInclusion.inclusion_month <= months[-1],
        )
    stored_inclusions = db.scalars(inclusion_query).all()
    inclusion_lookup: dict[tuple[int, str, int], float | None] = {}
    for line in stored_inclusions:
        inclusion_lookup[
            (
                line.ration_id,
                _month_start(line.inclusion_month).isoformat(),
                line.ingredient_id,
            )
        ] = line.kg_per_head

    carried_from_prior = (
        _latest_inclusions_before(
            db,
            ration_ids=[r["id"] for r in rations],
            before=months[0],
        )
        if months
        else {}
    )

    ration_payloads = []
    for ration in rations:
        ingredient_ids = ration["ingredient_ids"]
        last_kg: dict[int, float] = {
            ingredient_id: carried_from_prior[key]
            for ingredient_id in ingredient_ids
            if (key := (ration["id"], ingredient_id)) in carried_from_prior
        }
        rows = []
        for month_start in months:
            month_iso = month_start.isoformat()
            month_costs = costs_by_month.get(month_iso, {})
            entered: dict[str, float | None] = {}
            inclusions: dict[str, float | None] = {}
            for ingredient_id in ingredient_ids:
                key = str(ingredient_id)
                stored = inclusion_lookup.get(
                    (ration["id"], month_iso, ingredient_id)
                )
                entered[key] = stored
                if stored is not None:
                    last_kg[ingredient_id] = stored
                    inclusions[key] = stored
                else:
                    inclusions[key] = last_kg.get(ingredient_id)
            rows.append({
                "inclusion_month": month_iso,
                "month_label": month_start.strftime("%b-%y"),
                "inclusions": inclusions,
                "entered_inclusions": entered,
                "ingredient_costs": {
                    key: month_costs.get(key) for key in inclusions
                },
                "cost_per_head": _total_cost_per_head(
                    inclusions, month_costs, ingredient_ids
                ),
            })
        ration_payloads.append({**ration, "rows": rows})

    active_id = ration_id
    if active_id is None and ration_payloads:
        active_id = ration_payloads[0]["id"]
    elif active_id is not None and not any(r["id"] == active_id for r in ration_payloads):
        active_id = ration_payloads[0]["id"] if ration_payloads else None

    return {
        "farm": farm_code,
        **ration_period_meta(months, fiscal_year),
        "available_ingredients": available_ingredients,
        "rations": ration_payloads,
        "active_ration_id": active_id,
    }


def save_farm_ration_inclusions(
    db: Session,
    *,
    farm: str,
    ration_id: int,
    fiscal_year: int | None,
    rows: list[dict[str, Any]],
    user_id: int | None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    farm_code = normalize_farm_code(farm)
    _get_active_ration(db, ration_id=ration_id, farm=farm_code)
    ingredient_ids = set(_ration_ingredient_ids(db, ration_id))
    months = ration_month_range(
        fiscal_year=fiscal_year, month_from=month_from, month_to=month_to
    )
    valid_months = {m.isoformat() for m in months}

    for row in rows:
        month_raw = row.get("inclusion_month")
        ingredient_id = row.get("ingredient_id")
        if not month_raw or ingredient_id not in ingredient_ids:
            continue
        if isinstance(month_raw, dt.date):
            inclusion_month = _month_start(month_raw)
        else:
            inclusion_month = _month_start(dt.date.fromisoformat(str(month_raw)))
        if inclusion_month.isoformat() not in valid_months:
            continue

        kg_raw = row.get("kg_per_head")
        kg: float | None
        if kg_raw is None or kg_raw == "":
            kg = None
        else:
            kg = float(kg_raw)

        row_fy = (
            fiscal_year
            if fiscal_year is not None
            else _fiscal_year_from_date(inclusion_month)
        )
        existing = db.scalar(
            select(FarmRationInclusion).where(
                FarmRationInclusion.fiscal_year == row_fy,
                FarmRationInclusion.inclusion_month == inclusion_month,
                FarmRationInclusion.ration_id == ration_id,
                FarmRationInclusion.ingredient_id == ingredient_id,
            )
        )
        if kg is None:
            if existing:
                db.delete(existing)
            continue
        if existing:
            existing.kg_per_head = kg
            existing.updated_by_user_id = user_id
        else:
            db.add(
                FarmRationInclusion(
                    fiscal_year=row_fy,
                    inclusion_month=inclusion_month,
                    ration_id=ration_id,
                    ingredient_id=ingredient_id,
                    kg_per_head=kg,
                    updated_by_user_id=user_id,
                )
            )

    db.commit()
    return get_farm_ration_workbook(
        db,
        farm=farm_code,
        fiscal_year=fiscal_year,
        ration_id=ration_id,
        month_from=months[0] if months else month_from,
        month_to=months[-1] if months else month_to,
    )


def ration_base_name(name: str, farm: str) -> str | None:
    """Strip farm prefix from ration name, e.g. CM Milkers -> Milkers."""
    farm_code = normalize_farm_code(farm)
    stripped = name.strip()
    prefix = f"{farm_code} "
    if stripped.upper().startswith(prefix.upper()):
        rest = stripped[len(prefix) :].strip()
        return rest or None
    return None


def ration_suffix_key(base_name: str) -> str | None:
    """Map ration base name to feed forecast suffix key."""
    normalized = " ".join(base_name.strip().lower().split())
    compact = normalized.replace(" ", "")
    for key, patterns in RATION_SUFFIX_PATTERNS.items():
        for pattern in patterns:
            p = pattern.lower()
            if normalized == p or compact == p.replace(" ", ""):
                return key
    return None


def category_cost_per_head_day(
    row: dict[str, Any],
    ration: dict[str, Any],
    category: str,
) -> float | None:
    """£/head/day for one ingredient category within a ration month row."""
    inclusions = row.get("inclusions", {})
    costs = row.get("ingredient_costs", {})
    total = 0.0
    has_value = False
    for ing in ration.get("ingredients", []):
        if ing.get("category") != category:
            continue
        key = str(ing["id"])
        kg = inclusions.get(key)
        if kg is None:
            continue
        cost = costs.get(key)
        if cost is None:
            return None
        total += (float(kg) / 1000.0) * float(cost)
        has_value = True
    return round(total, 4) if has_value else None


def ration_costs_by_suffix(
    db: Session,
    *,
    farm: str,
    fiscal_year: int,
) -> dict[str, dict[str, dict[str, float | None]]]:
    """Per-month concentrate/forage/straw £/head/day by ration suffix key."""
    workbook = get_farm_ration_workbook(db, farm=farm, fiscal_year=fiscal_year)
    result: dict[str, dict[str, dict[str, float | None]]] = {
        key: {} for key in RATION_SUFFIX_PATTERNS
    }
    for ration in workbook["rations"]:
        base = ration_base_name(ration["name"], farm)
        if not base:
            continue
        suffix = ration_suffix_key(base)
        if suffix is None:
            continue
        for row in ration.get("rows", []):
            month_iso = row["inclusion_month"]
            result[suffix][month_iso] = {
                category: category_cost_per_head_day(row, ration, category)
                for category in FEED_INGREDIENT_CATEGORIES
            }
    return result


def _index_rations_by_base(
    rations: list[dict[str, Any]], farm: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for ration in rations:
        base = ration_base_name(ration["name"], farm)
        if base:
            indexed[base.lower()] = ration
    return indexed


def get_ration_cost_comparison(
    db: Session,
    *,
    fiscal_year: int | None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    cm_workbook = get_farm_ration_workbook(
        db,
        farm="CM",
        fiscal_year=fiscal_year,
        month_from=month_from,
        month_to=month_to,
    )
    gad_workbook = get_farm_ration_workbook(
        db,
        farm="GAD",
        fiscal_year=fiscal_year,
        month_from=month_from,
        month_to=month_to,
    )
    cm_index = _index_rations_by_base(cm_workbook["rations"], "CM")
    gad_index = _index_rations_by_base(gad_workbook["rations"], "GAD")
    months = ration_month_range(
        fiscal_year=fiscal_year, month_from=month_from, month_to=month_to
    )

    comparisons: list[dict[str, Any]] = []
    for key in sorted(set(cm_index) | set(gad_index)):
        cm_ration = cm_index.get(key)
        gad_ration = gad_index.get(key)
        base_label = (
            ration_base_name(cm_ration["name"], "CM")
            if cm_ration
            else ration_base_name(gad_ration["name"], "GAD")
        )
        rows: list[dict[str, Any]] = []
        for month_start in months:
            month_iso = month_start.isoformat()
            cm_row = next(
                (row for row in (cm_ration or {}).get("rows", []) if row["inclusion_month"] == month_iso),
                None,
            )
            gad_row = next(
                (row for row in (gad_ration or {}).get("rows", []) if row["inclusion_month"] == month_iso),
                None,
            )
            cm_day = cm_row["cost_per_head"] if cm_row else None
            gad_day = gad_row["cost_per_head"] if gad_row else None
            diff_day: float | None = None
            if cm_day is not None and gad_day is not None:
                diff_day = round(gad_day - cm_day, 4)
            rows.append({
                "inclusion_month": month_iso,
                "month_label": month_start.strftime("%b-%y"),
                "cm": {
                    "ration_name": cm_ration["name"] if cm_ration else None,
                    "cost_per_head_day": cm_day,
                },
                "gad": {
                    "ration_name": gad_ration["name"] if gad_ration else None,
                    "cost_per_head_day": gad_day,
                },
                "diff_per_day": diff_day,
            })
        comparisons.append({
            "base_name": base_label,
            "cm_ration_name": cm_ration["name"] if cm_ration else None,
            "gad_ration_name": gad_ration["name"] if gad_ration else None,
            "rows": rows,
        })

    return {
        **ration_period_meta(months, fiscal_year),
        "comparisons": comparisons,
    }
