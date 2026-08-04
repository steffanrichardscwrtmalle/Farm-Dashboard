"""CTS cattle-on-holding reconcile API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import ACTION_CTS_SYNC, PAGE_STOCK_INVENTORY
from app.db import get_db
from app.models import HERD_FARM_OPTIONS, User
from app.services.cts_client import CtsError, cts_status
from app.services.cts_reconcile import reconcile_farms, sync_farms

router = APIRouter(prefix="/api/cts")


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


@router.get("/status")
def api_cts_status(
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
):
    return cts_status()


@router.get("/reconcile")
def api_cts_reconcile(
    farm: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
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
