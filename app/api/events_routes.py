"""Cow events report API."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.models import User
from app.services.events_common import EVENT_PAGE_TYPES, build_events_page_report

router = APIRouter(prefix="/api/events")


def _events_report(
    page_slug: str,
    farm: list[str],
    event_from: dt.date | None,
    event_to: dt.date | None,
    db: Session,
    lact: list[str] | None = None,
    parity: list[str] | None = None,
) -> dict:
    if page_slug not in EVENT_PAGE_TYPES:
        raise HTTPException(status_code=404, detail="Unknown events page")
    farms = farm or None
    return build_events_page_report(
        db,
        page_slug=page_slug,
        farms=farms,
        event_from=event_from,
        event_to=event_to,
        lact_groups=lact,
        parity_groups=parity,
    )


@router.get("/calvings")
def api_calvings(
    farm: list[str] = Query(default=[]),
    lact: list[str] = Query(default=[]),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _events_report("calvings", farm, event_from, event_to, db, lact=lact or None)


@router.get("/sales")
def api_sales(
    farm: list[str] = Query(default=[]),
    parity: list[str] = Query(default=[]),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _events_report("sales", farm, event_from, event_to, db, parity=parity or None)


@router.get("/deaths")
def api_deaths(
    farm: list[str] = Query(default=[]),
    parity: list[str] = Query(default=[]),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _events_report("deaths", farm, event_from, event_to, db, parity=parity or None)


@router.get("/disease")
def api_disease(
    farm: list[str] = Query(default=[]),
    parity: list[str] = Query(default=[]),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _events_report("disease", farm, event_from, event_to, db, parity=parity or None)


@router.get("/breedings")
def api_breedings(
    farm: list[str] = Query(default=[]),
    parity: list[str] = Query(default=[]),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _events_report("breedings", farm, event_from, event_to, db, parity=parity or None)
