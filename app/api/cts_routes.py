"""CTS cattle-on-holding reconcile API."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import ACTION_CTS_SYNC, PAGE_BCMS
from app.db import get_db
from app.models import HERD_FARM_OPTIONS, User
from app.services.bcms_health import get_bcms_health
from app.services.cts_client import CtsError, cts_status
from app.services.cts_movements import (
    list_awaiting_cts_movements,
    list_pending_movements,
)
from app.services.cts_reconcile import reconcile_farms, sync_farms
from app.services.cts_submit import CtsSubmitError, send_pending_movements
from app.services.cts_submit_xml import CtsSubmitXmlError, build_preview_xml

router = APIRouter(prefix="/api/cts")


class SendMovementsBody(BaseModel):
    farm: str = Field(..., min_length=1)
    ids: list[str] = Field(default_factory=list)


class PreviewMovementsBody(BaseModel):
    farm: str = Field(..., min_length=1)
    ids: list[str] = Field(default_factory=list)
    kind: Literal["births", "movements"]


def _normalize_farms(farm: list[str]) -> list[str] | None:
    cleaned = [f.strip().upper() for f in farm if f and f.strip()]
    if not cleaned:
        return None
    invalid = [f for f in cleaned if f not in HERD_FARM_OPTIONS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid farm(s): {', '.join(invalid)}. Use CM and/or GAD.",
        )
    return cleaned


def _selected_pending_rows(
    db: Session, *, farm_key: str, ids: list[str]
) -> list[dict[str, Any]]:
    pending = list_pending_movements(db, farm_key)
    selected_ids = {item.strip() for item in ids if item and item.strip()}
    if selected_ids:
        rows = [row for row in pending["rows"] if row["id"] in selected_ids]
    else:
        rows = list(pending["rows"])
    holding = pending.get("holding") or ""
    for row in rows:
        row.setdefault("holding", holding)
    return rows


@router.get("/status")
def api_cts_status(
    _: User = Depends(require_page(PAGE_BCMS)),
):
    return cts_status()


@router.get("/health")
def api_cts_health(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BCMS)),
) -> dict[str, Any]:
    return get_bcms_health(db)


@router.get("/reconcile")
def api_cts_reconcile(
    farm: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BCMS)),
):
    return reconcile_farms(db, farms=_normalize_farms(farm))


@router.post("/sync")
def api_cts_sync(
    farm: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_CTS_SYNC)),
):
    try:
        return sync_farms(db, farms=_normalize_farms(farm), source="manual")
    except CtsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/movements/pending")
def api_cts_movements_pending(
    farm: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BCMS)),
) -> dict[str, Any]:
    farm_key = farm.strip().upper()
    if farm_key not in HERD_FARM_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid farm. Use CM or GAD.")
    try:
        return list_pending_movements(db, farm_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/movements/awaiting-cts")
def api_cts_movements_awaiting_cts(
    farm: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BCMS)),
) -> dict[str, Any]:
    farm_key = farm.strip().upper()
    if farm_key not in HERD_FARM_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid farm. Use CM or GAD.")
    try:
        return list_awaiting_cts_movements(db, farm_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/movements/preview")
def api_cts_movements_preview(
    body: PreviewMovementsBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BCMS)),
) -> Response:
    farm_key = body.farm.strip().upper()
    if farm_key not in HERD_FARM_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid farm. Use CM or GAD.")
    rows = _selected_pending_rows(db, farm_key=farm_key, ids=body.ids)
    if not rows:
        raise HTTPException(status_code=400, detail="No pending movements selected.")
    try:
        filename, xml = build_preview_xml(
            rows, farm=farm_key, kind=body.kind, redact_password=True
        )
    except CtsSubmitXmlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/movements/send")
def api_cts_movements_send(
    body: SendMovementsBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_CTS_SYNC)),
) -> dict[str, Any]:
    farm_key = body.farm.strip().upper()
    if farm_key not in HERD_FARM_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid farm. Use CM or GAD.")
    rows = _selected_pending_rows(db, farm_key=farm_key, ids=body.ids)
    if not rows:
        raise HTTPException(status_code=400, detail="No pending movements selected.")
    try:
        return send_pending_movements(db, farm=farm_key, rows=rows)
    except (CtsSubmitError, CtsError, CtsSubmitXmlError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
