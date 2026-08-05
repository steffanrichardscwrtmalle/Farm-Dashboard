"""Stock inventory report API."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.deps import require_page
from app.auth.permissions import PAGE_STOCK_INVENTORY
from app.db import get_db
from app.models import HERD_FARM_OPTIONS, User
from app.services.beef_inventory import (
    build_beef_inventory_csv,
    build_beef_inventory_pdf,
    get_beef_inventory_report,
)
from app.services.calves_due import get_calves_due_report
from app.services.heifer_inventory import (
    PDF_CONTENT_TYPE,
    build_heifer_inventory_csv,
    build_heifer_inventory_pdf,
    get_heifer_inventory_report,
)
from app.services.heifers_due import get_heifers_due_report

router = APIRouter(prefix="/api/stock-inventory")


@router.get("/heifer-inventory")
def api_heifer_inventory(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    farms = farm or None
    return get_heifer_inventory_report(
        db,
        farms=farms,
        min_age=min_age,
        max_age=max_age,
    )


def _selected_farms(farm: list[str]) -> list[str]:
    return farm or list(HERD_FARM_OPTIONS)


@router.get("/heifer-inventory/export.csv")
def api_heifer_inventory_export_csv(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    report = get_heifer_inventory_report(
        db, farms=farm or None, min_age=min_age, max_age=max_age
    )
    content = build_heifer_inventory_csv(report, _selected_farms(farm))
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="heifer_inventory.csv"'},
    )


@router.get("/heifer-inventory/export.pdf")
def api_heifer_inventory_export_pdf(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    report = get_heifer_inventory_report(
        db, farms=farm or None, min_age=min_age, max_age=max_age
    )
    content = build_heifer_inventory_pdf(report, _selected_farms(farm))
    return Response(
        content=content,
        media_type=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": 'attachment; filename="heifer_inventory.pdf"'},
    )


@router.get("/beef-inventory")
def api_beef_inventory(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    jv_mode: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    try:
        return get_beef_inventory_report(
            db,
            farms=farm or None,
            min_age=min_age,
            max_age=max_age,
            jv_mode=jv_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/beef-inventory/export.csv")
def api_beef_inventory_export_csv(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    jv_mode: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    try:
        report = get_beef_inventory_report(
            db,
            farms=farm or None,
            min_age=min_age,
            max_age=max_age,
            jv_mode=jv_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = build_beef_inventory_csv(report, _selected_farms(farm))
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beef_inventory.csv"'},
    )


@router.get("/beef-inventory/export.pdf")
def api_beef_inventory_export_pdf(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    jv_mode: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    try:
        report = get_beef_inventory_report(
            db,
            farms=farm or None,
            min_age=min_age,
            max_age=max_age,
            jv_mode=jv_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = build_beef_inventory_pdf(report, _selected_farms(farm))
    return Response(
        content=content,
        media_type=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": 'attachment; filename="beef_inventory.pdf"'},
    )


@router.get("/calves-due")
def api_calves_due(
    farm: list[str] = Query(default=[]),
    breed: list[str] = Query(default=[]),
    due_from: dt.date | None = Query(default=None),
    due_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    farms = farm or None
    breeds = breed or None
    return get_calves_due_report(
        db,
        farms=farms,
        breeds=breeds,
        due_from=due_from,
        due_to=due_to,
    )


@router.get("/heifers-due")
def api_heifers_due(
    farm: list[str] = Query(default=[]),
    due_from: dt.date | None = Query(default=None),
    due_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    farms = farm or None
    return get_heifers_due_report(
        db,
        farms=farms,
        due_from=due_from,
        due_to=due_to,
    )
