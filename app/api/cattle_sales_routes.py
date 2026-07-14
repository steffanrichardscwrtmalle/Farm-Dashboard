"""Cattle sales API (list, status, import from email)."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_action, require_page
from app.auth.import_key import require_import_or_any_action
from app.auth.permissions import ACTION_CATTLE_SALES_IMPORT, PAGE_CATTLE_SALES
from app.db import SessionLocal, get_db
from app.models import CattleSaleLine, User
from app.services.cattle_sales import list_cattle_sales
from app.services.cattle_sales_import import (
    cattle_sales_is_configured,
    get_import_status,
    import_cattle_sales,
    is_import_running,
    mark_import_started,
    run_import_in_background,
    upload_cattle_sale_pdfs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cattle-sales")


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


@router.get("")
def api_cattle_sales_list(
    farm: list[str] | None = Query(None),
    farms: str | None = Query(None),
    category: list[str] | None = Query(None),
    categories: str | None = Query(None),
    buyer: list[str] | None = Query(None),
    buyers: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    include_unmatched: bool = Query(True),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_CATTLE_SALES)),
):
    farm_list: list[str] | None = list(farm) if farm else None
    if not farm_list and farms:
        farm_list = [f for f in (part.strip() for part in farms.split(",")) if f]

    category_list: list[str] | None = list(category) if category else None
    if not category_list and categories:
        category_list = [
            c for c in (part.strip() for part in categories.split(",")) if c
        ]

    buyer_list: list[str] | None = list(buyer) if buyer else None
    if not buyer_list and buyers:
        buyer_list = [b for b in (part.strip() for part in buyers.split(",")) if b]

    return list_cattle_sales(
        db,
        farms=farm_list,
        categories=category_list,
        buyers=buyer_list,
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        include_unmatched=include_unmatched,
    )


@router.get("/status")
def api_cattle_sales_status(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(CattleSaleLine)) or 0
    latest_import = db.scalar(select(func.max(CattleSaleLine.imported_at)))
    latest_sale = db.scalar(select(func.max(CattleSaleLine.sale_date)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_sale_date": latest_sale.isoformat() if latest_sale else None,
        "import_status": get_import_status(),
    }


@router.get("/import/status")
def api_cattle_sales_import_status(
    _: User = Depends(get_current_user),
):
    return get_import_status()


@router.post("/import")
def api_import_cattle_sales(
    background_tasks: BackgroundTasks,
    full_history: bool = Query(False),
    days: int | None = Query(None, ge=1),
    _: None = Depends(require_import_or_any_action(ACTION_CATTLE_SALES_IMPORT)),
):
    if not cattle_sales_is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Cattle sales import is not configured. "
                "Set Graph API variables or LOCAL_CATTLE_SALES_DIR."
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
    return {"status": "started", "message": "Cattle sales import started."}


@router.post("/upload")
async def api_upload_cattle_sales(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_CATTLE_SALES_IMPORT)),
):
    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        payloads.append((upload.filename or "upload.pdf", content))
    try:
        return upload_cattle_sale_pdfs(db, payloads)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
