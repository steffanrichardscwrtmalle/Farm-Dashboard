"""Stock inventory report API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.models import User
from app.services.calves_due import get_calves_due_report
from app.services.heifer_inventory import get_heifer_inventory_report
from app.services.heifers_due import get_heifers_due_report

router = APIRouter(prefix="/api/stock-inventory")


@router.get("/heifer-inventory")
def api_heifer_inventory(
    farm: list[str] = Query(default=[]),
    min_age: int | None = Query(default=None, ge=0),
    max_age: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    farms = farm or None
    return get_heifer_inventory_report(
        db,
        farms=farms,
        min_age=min_age,
        max_age=max_age,
    )


@router.get("/calves-due")
def api_calves_due(
    farm: list[str] = Query(default=[]),
    breed: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    farms = farm or None
    breeds = breed or None
    return get_calves_due_report(db, farms=farms, breeds=breeds)


@router.get("/heifers-due")
def api_heifers_due(
    farm: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    farms = farm or None
    return get_heifers_due_report(db, farms=farms)
