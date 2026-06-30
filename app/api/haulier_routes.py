"""Milk haulier collections API (list, export, import from email)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.import_key import require_import_or_action
from app.auth.permissions import ACTION_MILK_COLLECTIONS_IMPORT, PAGE_MILK_QUALITY
from app.db import get_db
from app.models import MilkCollection, User
from app.services.haulier_collections import (
    XLSX_CONTENT_TYPE,
    build_collections_csv,
    build_collections_xlsx,
    list_collections,
)
from app.services.haulier_import import import_haulier_collections

router = APIRouter(prefix="/api/haulier")


@router.get("/collections")
def api_haulier_collections(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    return list_collections(db, farms=farm, date_from=date_from, date_to=date_to)


@router.get("/collections/export.csv")
def api_haulier_collections_export_csv(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    result = list_collections(db, farms=farm, date_from=date_from, date_to=date_to)
    content = build_collections_csv(result["rows"])
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="milk_collections.csv"'},
    )


@router.get("/collections/export.xlsx")
def api_haulier_collections_export_xlsx(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    result = list_collections(db, farms=farm, date_from=date_from, date_to=date_to)
    content = build_collections_xlsx(result["rows"])
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="milk_collections.xlsx"'
        },
    )


@router.get("/status")
def api_haulier_status(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(MilkCollection)) or 0
    latest_import = db.scalar(select(func.max(MilkCollection.imported_at)))
    latest_date = db.scalar(select(func.max(MilkCollection.collection_date)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_collection_date": latest_date.isoformat() if latest_date else None,
    }


@router.post("/import")
def api_import_haulier_collections(
    full_history: bool = Query(False),
    days: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_MILK_COLLECTIONS_IMPORT)),
):
    try:
        return import_haulier_collections(db, full_history=full_history, days=days)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
