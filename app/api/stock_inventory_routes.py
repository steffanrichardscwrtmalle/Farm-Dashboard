"""Stock inventory report API."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import require_page
from app.auth.permissions import PAGE_STOCK_INVENTORY
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
    _: User = Depends(require_page(PAGE_STOCK_INVENTORY)),
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
