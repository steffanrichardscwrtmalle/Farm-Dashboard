"""Feed rate API (Feedlync import + report)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.permissions import PAGE_FEED_RATE
from app.db import SessionLocal, get_db
from app.models import FeedRateRecord, User
from app.services.feed_rate_import import (
    get_feed_rate_report,
    get_import_status,
    is_import_running,
    mark_import_started,
    run_import_in_background,
)

router = APIRouter(prefix="/api/feed-rate")


@router.get("")
def api_feed_rate_report(
    ration: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return get_feed_rate_report(db, ration=ration or None)


@router.get("/import/status")
def api_feed_rate_import_status(
    _: User = Depends(get_current_user),
):
    return get_import_status()


@router.post("/import")
def api_feed_rate_import(
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_user),
):
    if is_import_running():
        return {"status": "running", "message": "Import already in progress."}

    mark_import_started()
    background_tasks.add_task(run_import_in_background, SessionLocal)
    return {"status": "started", "message": "Feedlync import started."}


@router.get("/status")
def api_feed_rate_status(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    row_count = db.scalar(select(func.count()).select_from(FeedRateRecord)) or 0
    latest_import = db.scalar(select(func.max(FeedRateRecord.import_timestamp)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "import_status": get_import_status(),
    }
