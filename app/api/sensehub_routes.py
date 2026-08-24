"""SenseHub report API (import + stored snapshots)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_action, require_page
from app.auth.permissions import ACTION_SENSEHUB_IMPORT, PAGE_SENSEHUB
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
    cull_tags_to_remove,
    get_youngstock_job_status,
    import_youngstock_health,
    is_youngstock_job_running,
    list_low_health,
    list_tags_to_remove,
    list_unassigned_calves,
    run_backfill_in_background,
    save_scr_tag,
)

router = APIRouter(prefix="/api/sensehub")


class ScrTagBody(BaseModel):
    row_key: str
    farm: str | None = None
    cow_id: str | None = None
    etag: str | None = None
    scr_tag: str | None = None


class CullAnimalBody(BaseModel):
    animal_id: int


class CullSelectedBody(BaseModel):
    animal_ids: list[int]


@router.get("/tags-to-remove")
def api_sensehub_tags_to_remove(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    try:
        return list_tags_to_remove(db)
    except SenseHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tags-to-remove/cull-all")
def api_sensehub_cull_tags_to_remove(
    body: CullSelectedBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_SENSEHUB_IMPORT)),
):
    try:
        return cull_tags_to_remove(db, animal_ids=body.animal_ids)
    except SenseHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tags-to-remove/cull")
def api_sensehub_cull_one_tag_to_remove(
    body: CullAnimalBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_SENSEHUB_IMPORT)),
):
    try:
        return cull_tags_to_remove(db, animal_id=body.animal_id)
    except SenseHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/unassigned")
def api_sensehub_unassigned(
    category: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    return list_unassigned_calves(db, categories=category)


@router.post("/unassigned/scr-tag")
def api_sensehub_save_scr_tag(
    body: ScrTagBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SENSEHUB)),
):
    try:
        return save_scr_tag(
            db,
            row_key=body.row_key,
            farm=body.farm,
            cow_id=body.cow_id,
            etag=body.etag,
            scr_tag=body.scr_tag,
        )
    except (ValueError, SenseHubError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    days: int | None = Query(None, ge=1, le=730),
    _: User = Depends(require_action(ACTION_SENSEHUB_IMPORT)),
):
    if is_youngstock_job_running():
        return {"status": "running", "message": "A SenseHub backfill is already running."}
    background_tasks.add_task(
        run_backfill_in_background, SessionLocal, days, force=True
    )
    if days is None:
        message = "Re-downloading all SenseHub youngstock history…"
    else:
        message = f"Re-downloading the last {days} days from SenseHub…"
    return {
        "status": "started",
        "message": message,
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
    _: User = Depends(require_action(ACTION_SENSEHUB_IMPORT)),
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
    _: User = Depends(require_action(ACTION_SENSEHUB_IMPORT)),
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
