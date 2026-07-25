"""Parlour API: milk-flow import and shift summary."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.import_key import require_import_or_action
from app.auth.permissions import ACTION_PARLOUR_IMPORT, PAGE_PARLOUR
from app.db import SessionLocal, get_db
from app.models import ParlourMilkFlowImport, User
from app.services.parlour_email_import import (
    get_import_status,
    is_import_running,
    mark_import_started,
    parlour_is_configured,
    run_import_in_background,
)
from app.services.parlour_milk_flow_import import upload_milk_flow_files
from app.services.parlour_rotation import (
    DEFAULT_MA_WINDOW,
    list_rotation_series,
    rotation_date_bounds,
)
from app.services.parlour_scatter import (
    ATTACHMENT_METRIC_KEY,
    SCATTER_METRIC_KEYS,
    list_attachment_time_bins,
    list_scatter_metrics,
    list_scatter_points,
    scatter_date_bounds,
)
from app.services.parlour_shift_summary import (
    TREND_METRIC_KEYS,
    list_shift_summaries,
    milking_point_metric_trend,
    pen_metric_trend,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/parlour")


@router.get("/status")
def api_parlour_status(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    import_count = db.scalar(select(func.count()).select_from(ParlourMilkFlowImport)) or 0
    latest = db.scalar(select(func.max(ParlourMilkFlowImport.imported_at)))
    latest_date = db.scalar(select(func.max(ParlourMilkFlowImport.milking_date)))
    return {
        "import_count": import_count,
        "latest_import": latest.isoformat() if latest else None,
        "latest_milking_date": latest_date.isoformat() if latest_date else None,
        "import_status": get_import_status(),
        "email_configured": parlour_is_configured(),
    }


@router.get("/shift-summary")
def api_parlour_shift_summary(
    farm: str | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    shift: str | None = Query(None),
    include_pens: bool = Query(False),
    include_milking_points: bool = Query(False),
    include_problem_stalls: bool = Query(False),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper() if farm else None
    if farm_key and farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    return list_shift_summaries(
        db,
        farm=farm_key,
        date_from=date_from,
        date_to=date_to,
        shift=shift,
        include_pens=include_pens,
        include_milking_points=include_milking_points,
        include_problem_stalls=include_problem_stalls,
    )


@router.get("/milking-point-trend")
def api_parlour_milking_point_trend(
    farm: str = Query(...),
    metric: str = Query(...),
    milking_point: int | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    if metric not in TREND_METRIC_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"metric must be one of: {', '.join(sorted(TREND_METRIC_KEYS))}",
        )
    try:
        return milking_point_metric_trend(
            db,
            farm=farm_key,
            milking_point=milking_point,
            metric=metric,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pen-trend")
def api_parlour_pen_trend(
    farm: str = Query(...),
    metric: str = Query(...),
    pen: int | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    if metric not in TREND_METRIC_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"metric must be one of: {', '.join(sorted(TREND_METRIC_KEYS))}",
        )
    try:
        return pen_metric_trend(
            db,
            farm=farm_key,
            pen=pen,
            metric=metric,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/scatter/metrics")
def api_parlour_scatter_metrics(
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    return {"metrics": list_scatter_metrics()}


def _parse_shift_list(shifts: str | None) -> list[str] | None:
    if shifts is None:
        return None
    if shifts.strip() == "":
        return []
    return [part.strip() for part in shifts.split(",") if part.strip()]


@router.get("/scatter")
def api_parlour_scatter(
    farm: str = Query(...),
    metric: str = Query("yield_kg"),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    shifts: str | None = Query(
        None,
        description="Comma-separated shifts. Omit for all; empty string for none.",
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    shift_list = _parse_shift_list(shifts)

    if metric == ATTACHMENT_METRIC_KEY:
        return list_attachment_time_bins(
            db,
            farm=farm_key,
            date_from=date_from,
            date_to=date_to,
            shifts=shift_list,
        )

    if metric not in SCATTER_METRIC_KEYS:
        allowed = ", ".join(
            sorted({*SCATTER_METRIC_KEYS, ATTACHMENT_METRIC_KEY})
        )
        raise HTTPException(
            status_code=400,
            detail=f"metric must be one of: {allowed}",
        )

    try:
        return list_scatter_points(
            db,
            farm=farm_key,
            metric=metric,
            date_from=date_from,
            date_to=date_to,
            shifts=shift_list,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/scatter/bounds")
def api_parlour_scatter_bounds(
    farm: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper() if farm else None
    if farm_key and farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    return scatter_date_bounds(db, farm=farm_key)


@router.get("/scatter/attachment-bins")
def api_parlour_attachment_bins(
    farm: str = Query(...),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    shifts: str | None = Query(
        None,
        description="Comma-separated shifts. Omit for all; empty string for none.",
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    return list_attachment_time_bins(
        db,
        farm=farm_key,
        date_from=date_from,
        date_to=date_to,
        shifts=_parse_shift_list(shifts),
    )


@router.get("/rotation/bounds")
def api_parlour_rotation_bounds(
    farm: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper() if farm else None
    if farm_key and farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    return rotation_date_bounds(db, farm=farm_key)


@router.get("/rotation")
def api_parlour_rotation(
    farm: str = Query(...),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    shifts: str | None = Query(
        None,
        description="Comma-separated shifts. Omit for all; empty string for none.",
    ),
    ma_window: int = Query(DEFAULT_MA_WINDOW, ge=5, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_PARLOUR)),
):
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")
    if shifts is None:
        shift_list = None
    elif shifts.strip() == "":
        shift_list = []
    else:
        shift_list = [part.strip() for part in shifts.split(",") if part.strip()]
    return list_rotation_series(
        db,
        farm=farm_key,
        date_from=date_from,
        date_to=date_to,
        shifts=shift_list,
        ma_window=ma_window,
    )


@router.get("/import/status")
def api_parlour_import_status(
    _: None = Depends(require_import_or_action(ACTION_PARLOUR_IMPORT)),
):
    return get_import_status()


@router.post("/import")
def api_parlour_import(
    background_tasks: BackgroundTasks,
    full_history: bool = Query(False),
    days: int | None = Query(None, ge=1),
    since_last_import: bool = Query(
        False,
        description="Scan only mail newer than the latest successful import.",
    ),
    _: None = Depends(require_import_or_action(ACTION_PARLOUR_IMPORT)),
):
    if not parlour_is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Parlour import is not configured. "
                "Set Graph API variables or LOCAL_PARLOUR_DIR."
            ),
        )
    if is_import_running():
        return {"status": "running", "message": "Import already in progress."}

    mark_import_started(days=days, since_last_import=since_last_import)
    background_tasks.add_task(
        run_import_in_background,
        SessionLocal,
        full_history=full_history,
        days=days,
        since_last_import=since_last_import,
    )
    message = (
        f"Scanning last {days} day(s)…"
        if days
        else (
            "Scanning since last import…"
            if since_last_import
            else "Parlour milk-flow import started."
        )
    )
    return {"status": "started", "message": message}


@router.post("/upload")
async def api_parlour_upload(
    files: list[UploadFile] = File(...),
    farm: str | None = Form(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_action(ACTION_PARLOUR_IMPORT)),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    farm_key = farm.upper().strip() if farm else None
    if farm_key and farm_key not in {"CM", "GAD"}:
        raise HTTPException(status_code=400, detail="farm must be CM or GAD")

    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        payloads.append((upload.filename or "upload.xls", content))
    try:
        result = upload_milk_flow_files(db, payloads, farm=farm_key)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Parlour milk flow upload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not result["results"] and result["errors"]:
        raise HTTPException(status_code=400, detail="; ".join(result["errors"]))
    return result
