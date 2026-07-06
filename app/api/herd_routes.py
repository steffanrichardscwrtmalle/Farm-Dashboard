"""Herd data import API (OneDrive CSV → database)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.import_key import require_import_or_action
from app.auth.permissions import ACTION_HERD_IMPORT
from app.db import get_db
from app.models import CowEvent, GenomicResult, HerdBirth, HerdInventory, User
from app.services.genomic_import import import_genomic_results
from app.services.herd_birth_import import import_herd_births
from app.services.herd_events_import import import_cow_events
from app.services.herd_inventory_import import import_herd_inventory

router = APIRouter(prefix="/api/herd")


def _import_error_handler(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("/events/import")
def api_import_cow_events(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_HERD_IMPORT)),
):
    try:
        return import_cow_events(db)
    except (FileNotFoundError, ValueError) as exc:
        raise _import_error_handler(exc) from exc


@router.get("/events/status")
def api_cow_events_status(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(CowEvent)) or 0
    latest_import = db.scalar(select(func.max(CowEvent.import_timestamp)))
    latest_event_date = db.scalar(select(func.max(CowEvent.event_date)))
    farm_counts = dict(
        db.execute(select(CowEvent.farm, func.count()).group_by(CowEvent.farm)).all()
    )
    return {
        "row_count": row_count,
        "farm_counts": farm_counts,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_event_date": latest_event_date.isoformat() if latest_event_date else None,
    }


@router.post("/inventory/import")
def api_import_herd_inventory(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_HERD_IMPORT)),
):
    try:
        return import_herd_inventory(db)
    except (FileNotFoundError, ValueError) as exc:
        raise _import_error_handler(exc) from exc


@router.get("/inventory/status")
def api_herd_inventory_status(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(HerdInventory)) or 0
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
    }


@router.post("/birth/import")
def api_import_herd_births(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_HERD_IMPORT)),
):
    try:
        return import_herd_births(db)
    except (FileNotFoundError, ValueError) as exc:
        raise _import_error_handler(exc) from exc


@router.get("/birth/status")
def api_herd_births_status(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(HerdBirth)) or 0
    latest_import = db.scalar(select(func.max(HerdBirth.import_timestamp)))
    latest_birth_date = db.scalar(select(func.max(HerdBirth.bdat)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_birth_date": latest_birth_date.isoformat() if latest_birth_date else None,
    }


@router.post("/genomic/import")
def api_import_genomic_results(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_HERD_IMPORT)),
):
    try:
        return import_genomic_results(db)
    except (FileNotFoundError, ValueError) as exc:
        raise _import_error_handler(exc) from exc


@router.get("/genomic/status")
def api_genomic_status(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(GenomicResult)) or 0
    latest_import = db.scalar(select(func.max(GenomicResult.updated_at)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
