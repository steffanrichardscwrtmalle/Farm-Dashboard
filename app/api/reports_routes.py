"""Farm Reports API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.deps import require_page
from app.auth.permissions import PAGE_REPORTS
from app.db import get_db
from app.models import User
from app.services.farm_reports import (
    PDF_CONTENT_TYPE,
    XLSX_CONTENT_TYPE,
    build_heifers_to_scan_pdf,
    build_heifers_to_scan_xlsx,
    farm_reports,
    heifers_to_scan,
)
from app.services.farm_schedule import normalize_farm

router = APIRouter(prefix="/api/reports")


def _farm_or_400(farm: str) -> str:
    try:
        return normalize_farm(farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{farm}/heifers-to-scan/export.pdf")
def api_heifers_to_scan_export_pdf(
    farm: str,
    pen: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_REPORTS)),
) -> Response:
    farm_key = _farm_or_400(farm)
    report = heifers_to_scan(db, farm_key, pens=pen or None)
    filename = f"heifers_to_scan_{farm_key.lower()}.pdf"
    return Response(
        content=build_heifers_to_scan_pdf(report),
        media_type=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{farm}/heifers-to-scan/export.xlsx")
def api_heifers_to_scan_export_xlsx(
    farm: str,
    pen: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_REPORTS)),
) -> Response:
    farm_key = _farm_or_400(farm)
    report = heifers_to_scan(db, farm_key, pens=pen or None)
    filename = f"heifers_to_scan_{farm_key.lower()}.xlsx"
    return Response(
        content=build_heifers_to_scan_xlsx(report),
        media_type=XLSX_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{farm}")
def api_farm_reports(
    farm: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_REPORTS)),
) -> dict[str, Any]:
    return farm_reports(db, _farm_or_400(farm))
