"""Herd data import API (OneDrive CSV → database)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.import_key import require_import_or_editor
from app.db import get_db
from app.models import CowEvent
from app.services.herd_events_import import import_cow_events

router = APIRouter(prefix="/api/herd")


@router.post("/events/import")
def api_import_cow_events(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_editor),
):
    try:
        return import_cow_events(db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/events/status")
def api_cow_events_status(
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_editor),
):
    row_count = db.scalar(select(func.count()).select_from(CowEvent)) or 0
    latest_import = db.scalar(select(func.max(CowEvent.import_timestamp)))
    latest_event_date = db.scalar(select(func.max(CowEvent.event_date)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_event_date": latest_event_date.isoformat() if latest_event_date else None,
    }
