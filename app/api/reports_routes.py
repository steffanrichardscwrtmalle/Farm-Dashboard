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
    REPORT_SPECS,
    XLSX_CONTENT_TYPE,
    build_report_pdf,
    build_report_xlsx,
    farm_reports,
    load_report,
)
from app.services.farm_schedule import normalize_farm

router = APIRouter(prefix="/api/reports")


def _farm_or_400(farm: str) -> str:
    try:
        return normalize_farm(farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _report_or_404(report_id: str) -> dict[str, Any]:
    spec = REPORT_SPECS.get(report_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown report")
    return spec


@router.get("/{farm}/{report_id}/export.pdf")
def api_report_export_pdf(
    farm: str,
    report_id: str,
    pen: list[str] = Query(default=[]),
    etag5_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_REPORTS)),
) -> Response:
    farm_key = _farm_or_400(farm)
    spec = _report_or_404(report_id)
    report = load_report(db, farm_key, report_id, pens=pen or None)
    suffix = "_etag5" if etag5_only else ""
    filename = f"{spec['filename']}{suffix}_{farm_key.lower()}.pdf"
    return Response(
        content=build_report_pdf(report, etag5_only=etag5_only),
        media_type=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{farm}/{report_id}/export.xlsx")
def api_report_export_xlsx(
    farm: str,
    report_id: str,
    pen: list[str] = Query(default=[]),
    etag5_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_REPORTS)),
) -> Response:
    farm_key = _farm_or_400(farm)
    spec = _report_or_404(report_id)
    report = load_report(db, farm_key, report_id, pens=pen or None)
    suffix = "_etag5" if etag5_only else ""
    filename = f"{spec['filename']}{suffix}_{farm_key.lower()}.xlsx"
    return Response(
        content=build_report_xlsx(report, etag5_only=etag5_only),
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
