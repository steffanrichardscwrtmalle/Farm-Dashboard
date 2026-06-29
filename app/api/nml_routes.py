"""NML milk-quality results API (list, export, import from email)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.import_key import require_import_or_action
from app.auth.permissions import ACTION_MILK_QUALITY_IMPORT, PAGE_MILK_QUALITY
from app.db import get_db
from app.models import NmlMilkResult, User
from app.services.nml_import import import_nml_results
from app.services.nml_results import (
    XLSX_CONTENT_TYPE,
    build_nml_results_csv,
    build_nml_results_xlsx,
    list_nml_results,
)

router = APIRouter(prefix="/api/nml")


@router.get("/results")
def api_nml_results(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    return list_nml_results(db, farms=farm, date_from=date_from, date_to=date_to)


@router.get("/results/export.csv")
def api_nml_results_export_csv(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    result = list_nml_results(db, farms=farm, date_from=date_from, date_to=date_to)
    content = build_nml_results_csv(result["rows"])
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="nml_milk_results.csv"'},
    )


@router.get("/results/export.xlsx")
def api_nml_results_export_xlsx(
    farm: list[str] | None = Query(None),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_MILK_QUALITY)),
):
    result = list_nml_results(db, farms=farm, date_from=date_from, date_to=date_to)
    content = build_nml_results_xlsx(result["rows"])
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="nml_milk_results.xlsx"'
        },
    )


@router.get("/status")
def api_nml_status(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    row_count = db.scalar(select(func.count()).select_from(NmlMilkResult)) or 0
    latest_import = db.scalar(select(func.max(NmlMilkResult.imported_at)))
    latest_sample = db.scalar(select(func.max(NmlMilkResult.sample_date)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "latest_sample_date": latest_sample.isoformat() if latest_sample else None,
    }


@router.post("/import")
def api_import_nml_results(
    full_history: bool = Query(False),
    db: Session = Depends(get_db),
    _: None = Depends(require_import_or_action(ACTION_MILK_QUALITY_IMPORT)),
):
    try:
        return import_nml_results(db, full_history=full_history)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
