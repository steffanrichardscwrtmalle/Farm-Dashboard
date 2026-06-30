"""Milk buyer statement API (list, status, import from email)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.import_key import require_import_or_any_action
from app.auth.permissions import (
    MILK_IMPORT_ACTIONS,
    PAGE_MILK_QUALITY,
)
from app.db import SessionLocal, get_db
from app.models import MilkStatement, User
from app.services.milk_statements import list_milk_statements
from app.services.milk_statements_import import (
    get_import_status,
    import_milk_statements,
    is_import_running,
    mark_import_started,
    run_import_in_background,
    statements_is_configured,
    upload_milk_statement_pdfs,
)

logger = logging.getLogger(__name__)

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
        "import_status": get_import_status(),
    }


@router.get("/import/status")
def api_milk_statements_import_status(
    _: User = Depends(get_current_user),
):
    return get_import_status()


@router.post("/import")
def api_import_milk_statements(
    background_tasks: BackgroundTasks,
    full_history: bool = Query(False),
    days: int | None = Query(None, ge=1),
    _: None = Depends(require_import_or_any_action(*MILK_IMPORT_ACTIONS)),
):
    """Start a mailbox scan in the background (Render HTTP requests time out ~30s)."""
    if not statements_is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Milk statements import is not configured. "
                "Set Graph API variables or LOCAL_STATEMENTS_DIR."
            ),
        )
    if is_import_running():
        return {"status": "running", "message": "Import already in progress."}

    mark_import_started(days=days)
    background_tasks.add_task(
        run_import_in_background,
        SessionLocal,
        full_history=full_history,
        days=days,
    )
    return {"status": "started", "message": "Statement import started."}


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
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Milk statements upload failed")
        raise HTTPException(
            status_code=500, detail=f"Upload failed: {type(exc).__name__}: {exc}"
        ) from exc
