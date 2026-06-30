"""Milk buyer statement API (list, status, import from email)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.import_key import require_import_or_any_action
from app.auth.permissions import (
    MILK_IMPORT_ACTIONS,
    PAGE_MILK_QUALITY,
    can_import_milk_statements,
)
from app.db import get_db
from app.models import MilkStatement, User
from app.services.milk_statements import list_milk_statements
from app.services.milk_statements_import import import_milk_statements, upload_milk_statement_pdfs

router = APIRouter(prefix="/api/milk-statements")


@router.get("/list")
def api_milk_statements_list(
    fiscal_year: int | None = Query(None),
    farms: str | None = Query(None),
    farm: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    raw = farms or farm
    farm_list = (
        [f for f in (part.strip() for part in raw.split(",")) if f] if raw else None
    )
    return list_milk_statements(db, fiscal_year=fiscal_year, farms=farm_list)


@router.get("/status")
def api_milk_statements_status(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(MilkStatement)) or 0
    latest_import = db.scalar(select(func.max(MilkStatement.imported_at)))
    latest_month = db.scalar(select(func.max(MilkStatement.statement_month)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_statement_month": latest_month.isoformat() if latest_month else None,
    }


@router.post("/import")
def api_import_milk_statements(
    full_history: bool = Query(False),
    days: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_any_action(*MILK_IMPORT_ACTIONS)),
):
    try:
        return import_milk_statements(db, full_history=full_history, days=days)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/upload")
async def api_upload_milk_statements(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_any_action(*MILK_IMPORT_ACTIONS)),
):
    if not files:
        raise HTTPException(status_code=400, detail="No PDF files provided")
    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        payloads.append((upload.filename or "upload.pdf", content))
    try:
        return upload_milk_statement_pdfs(db, payloads)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
