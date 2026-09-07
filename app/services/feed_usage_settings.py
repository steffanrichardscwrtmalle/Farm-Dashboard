"""Persisted Feed Usage ration-to-farm assignments."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    HERD_FARM_OPTIONS,
    FeedUsageIngredientAssignment,
    FeedUsageRationAssignment,
)
from app.services.feedlync_api import (
    USAGE_RATIONS_BY_FARM,
    fetch_ingredient_summaries,
    fetch_ration_summaries,
    usage_ingredient_key,
)
from app.services.feedlync_auth import FeedlyncAuthError


def _normalize_assignment_farm(farm: str | None) -> str:
    value = (farm or "").strip().upper()
    if not value:
        return ""
    if value not in HERD_FARM_OPTIONS:
        raise ValueError("Farm must be CM, GAD, or unassigned.")
    return value


def seed_ration_assignments_if_empty(db: Session) -> None:
    existing = db.scalar(select(FeedUsageRationAssignment.id).limit(1))
    if existing is not None:
        return
    now = dt.datetime.now()
    for farm, names in USAGE_RATIONS_BY_FARM.items():
        for name in names:
            db.add(
                FeedUsageRationAssignment(
                    ration_name=name,
                    farm=farm,
                    updated_at=now,
                )
            )
    db.commit()


def ration_farm_lookup(db: Session) -> dict[str, str]:
    rows = db.scalars(select(FeedUsageRationAssignment)).all()
    return {
        row.ration_name.casefold(): row.farm
        for row in rows
        if (row.farm or "") in HERD_FARM_OPTIONS
    }


def assigned_ration_names(db: Session, farm: str) -> list[str]:
    farm_key = _normalize_assignment_farm(farm)
    if not farm_key:
        return []
    rows = db.scalars(
        select(FeedUsageRationAssignment)
        .where(FeedUsageRationAssignment.farm == farm_key)
        .order_by(FeedUsageRationAssignment.ration_name)
    ).all()
    return [row.ration_name for row in rows]


def list_saved_ration_assignments(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(FeedUsageRationAssignment).order_by(FeedUsageRationAssignment.ration_name)
    ).all()
    return [row.to_dict() for row in rows]


def save_ration_assignment(
    db: Session,
    *,
    ration_name: str,
    farm: str | None,
    feedlync_ration_id: str | None = None,
) -> dict[str, Any]:
    name = (ration_name or "").strip()
    if not name:
        raise ValueError("Ration name is required.")
    farm_key = _normalize_assignment_farm(farm)
    row = db.scalar(
        select(FeedUsageRationAssignment).where(
            FeedUsageRationAssignment.ration_name == name
        )
    )
    if row is None:
        row = db.scalar(
            select(FeedUsageRationAssignment).where(
                FeedUsageRationAssignment.ration_name.ilike(name)
            )
        )
    now = dt.datetime.now()
    if row is None:
        row = FeedUsageRationAssignment(ration_name=name)
        db.add(row)
    else:
        row.ration_name = name
    row.farm = farm_key
    row.updated_at = now
    if feedlync_ration_id:
        row.feedlync_ration_id = feedlync_ration_id
    db.commit()
    db.refresh(row)
    return row.to_dict()


def _upsert_feedlync_ration(
    db: Session,
    *,
    name: str,
    feedlync_id: str,
) -> FeedUsageRationAssignment:
    row = db.scalar(
        select(FeedUsageRationAssignment).where(
            FeedUsageRationAssignment.ration_name == name
        )
    )
    if row is None:
        row = db.scalar(
            select(FeedUsageRationAssignment).where(
                FeedUsageRationAssignment.ration_name.ilike(name)
            )
        )
    if row is None:
        row = FeedUsageRationAssignment(ration_name=name, farm="", updated_at=dt.datetime.now())
        db.add(row)
    row.ration_name = name
    if feedlync_id:
        row.feedlync_ration_id = feedlync_id
    return row


def list_ration_assignments(
    db: Session,
    *,
    sync: bool = False,
) -> dict[str, Any]:
    seed_ration_assignments_if_empty(db)
    needs_auth = False
    sync_error = None
    if sync:
        try:
            for item in fetch_ration_summaries(db):
                _upsert_feedlync_ration(
                    db,
                    name=item["name"],
                    feedlync_id=item.get("id") or "",
                )
            db.commit()
        except FeedlyncAuthError as exc:
            db.rollback()
            needs_auth = True
            sync_error = str(exc)
        except Exception as exc:
            db.rollback()
            sync_error = str(exc)

    rows = list_saved_ration_assignments(db)
    return {
        "rations": rows,
        "needs_auth": needs_auth,
        "sync_error": sync_error,
        "synced": sync and not sync_error,
    }


def ingredient_inclusion_lookup(db: Session) -> dict[str, bool] | None:
    rows = list(db.scalars(select(FeedUsageIngredientAssignment)).all())
    if not rows:
        return None
    return {
        usage_ingredient_key(row.ingredient_name): bool(row.included) for row in rows
    }


def is_usage_ingredient_included(
    name: str, lookup: dict[str, bool] | None
) -> bool:
    if lookup is None:
        return True
    key = usage_ingredient_key(name)
    if key in lookup:
        return bool(lookup[key])
    return True


def save_ingredient_assignment(
    db: Session,
    *,
    ingredient_name: str,
    included: bool,
    feedlync_ingredient_id: str | None = None,
    ingredient_type_id: int | None = None,
    ingredient_type_name: str | None = None,
) -> dict[str, Any]:
    name = (ingredient_name or "").strip()
    if not name:
        raise ValueError("Ingredient name is required.")
    row = db.scalar(
        select(FeedUsageIngredientAssignment).where(
            FeedUsageIngredientAssignment.ingredient_name == name
        )
    )
    if row is None:
        row = db.scalar(
            select(FeedUsageIngredientAssignment).where(
                FeedUsageIngredientAssignment.ingredient_name.ilike(name)
            )
        )
    now = dt.datetime.now()
    if row is None:
        row = FeedUsageIngredientAssignment(ingredient_name=name)
        db.add(row)
    else:
        row.ingredient_name = name
    row.included = bool(included)
    row.updated_at = now
    if feedlync_ingredient_id:
        row.feedlync_ingredient_id = feedlync_ingredient_id
    if ingredient_type_id is not None:
        row.ingredient_type_id = ingredient_type_id
    if ingredient_type_name is not None:
        row.ingredient_type_name = ingredient_type_name
    db.commit()
    db.refresh(row)
    return row.to_dict()


def _upsert_feedlync_ingredient(
    db: Session,
    *,
    name: str,
    feedlync_id: str,
    ingredient_type_id: int | None,
    ingredient_type_name: str,
    is_forage: bool,
) -> FeedUsageIngredientAssignment:
    row = db.scalar(
        select(FeedUsageIngredientAssignment).where(
            FeedUsageIngredientAssignment.ingredient_name == name
        )
    )
    if row is None:
        row = db.scalar(
            select(FeedUsageIngredientAssignment).where(
                FeedUsageIngredientAssignment.ingredient_name.ilike(name)
            )
        )
    if row is None:
        row = FeedUsageIngredientAssignment(
            ingredient_name=name,
            included=not is_forage,
            updated_at=dt.datetime.now(),
        )
        db.add(row)
    row.ingredient_name = name
    if feedlync_id:
        row.feedlync_ingredient_id = feedlync_id
    row.ingredient_type_id = ingredient_type_id
    row.ingredient_type_name = ingredient_type_name or ""
    return row


def list_ingredient_assignments(
    db: Session,
    *,
    sync: bool = False,
) -> dict[str, Any]:
    needs_auth = False
    sync_error = None
    if sync:
        try:
            for item in fetch_ingredient_summaries(db):
                raw_type = item.get("ingredient_type_id")
                try:
                    type_id = int(raw_type) if raw_type is not None else None
                except (TypeError, ValueError):
                    type_id = None
                _upsert_feedlync_ingredient(
                    db,
                    name=str(item["name"]),
                    feedlync_id=str(item.get("id") or ""),
                    ingredient_type_id=type_id,
                    ingredient_type_name=str(item.get("ingredient_type_name") or ""),
                    is_forage=bool(item.get("is_forage")),
                )
            db.commit()
        except FeedlyncAuthError as exc:
            db.rollback()
            needs_auth = True
            sync_error = str(exc)
        except Exception as exc:
            db.rollback()
            sync_error = str(exc)

    rows = [
        row.to_dict()
        for row in db.scalars(
            select(FeedUsageIngredientAssignment).order_by(
                FeedUsageIngredientAssignment.ingredient_type_name,
                FeedUsageIngredientAssignment.ingredient_name,
            )
        ).all()
    ]
    return {
        "ingredients": rows,
        "needs_auth": needs_auth,
        "sync_error": sync_error,
        "synced": sync and not sync_error,
    }
