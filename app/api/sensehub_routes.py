"""SenseHub report API (import + stored snapshots)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.permissions import PAGE_SENSEHUB
from app.db import SessionLocal, get_db
from app.models import User
from app.services.sensehub_api import SenseHubError
from app.services.sensehub_import import (
    get_import_status,
    get_sensehub_report,
    is_import_running,
    mark_import_started,
    run_import_in_background,
)
from app.services.sensehub_youngstock import (
    DEFAULT_THRESHOLD,
    animal_events,
    get_youngstock_job_status,
    import_youngstock_health,
    is_youngstock_job_running,
    list_low_health,
    run_backfill_in_background,
)

router = APIRouter(prefix="/api/sensehub")


@router.get("/youngstock")
def api_sensehub_youngstock(
    threshold: float = Query(DEFAULT_THRESHOLD),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    return list_low_health(db, threshold=threshold)


@router.get("/youngstock/job")
def api_sensehub_youngstock_job(
    _: User = Depends(get_current_user),
):
    return get_youngstock_job_status()


@router.post("/youngstock/backfill")
def api_sensehub_youngstock_backfill(
    background_tasks: BackgroundTasks,
    days: int = Query(7, ge=1, le=14),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    if is_youngstock_job_running():
        return {"status": "running", "message": "A SenseHub backfill is already running."}
    background_tasks.add_task(run_backfill_in_background, SessionLocal, days)
    return {
        "status": "started",
        "message": f"Backfilling the last {days} days from SenseHub…",
        "days": days,
    }


@router.get("/youngstock/{animal_id}/events")
def api_sensehub_youngstock_events(
    animal_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    return animal_events(db, animal_id)


@router.post("/youngstock/import")
def api_sensehub_youngstock_import(
    _: User = Depends(require_page(PAGE_SENSEHUB)),
    db: Session = Depends(get_db),
):
    try:
        return import_youngstock_health(db)
    except SenseHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def api_sensehub_report(
    name: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    return get_sensehub_report(db, name=name or None)


@router.get("/import/status")
def api_sensehub_import_status(
    _: User = Depends(get_current_user),
):
    return get_import_status()


@router.post("/import")
def api_sensehub_import(
    background_tasks: BackgroundTasks,
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    if is_import_running():
        return {"status": "running", "message": "SenseHub import already in progress."}

    mark_import_started()
    background_tasks.add_task(run_import_in_background, SessionLocal)
    return {"status": "started", "message": "SenseHub import started."}


@router.get("/status")
def api_sensehub_status(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    report = get_sensehub_report(db)
    return {
        "configured": report["configured"],
        "latest_import": report["latest_import"],
        "farm_id": report["farm_id"],
        "farm_name": report["farm_name"],
        "report_count": len(report["reports"]),
        "import_status": get_import_status(),
    }


def _raise_sensehub(exc: SenseHubError) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
