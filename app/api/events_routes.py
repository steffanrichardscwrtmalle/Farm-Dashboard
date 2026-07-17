"""Cow events report API."""



from __future__ import annotations



import datetime as dt



from fastapi import APIRouter, Depends, HTTPException, Query

from pydantic import BaseModel, Field

from sqlalchemy.orm import Session



from app.auth.deps import get_current_user, require_admin, require_page
from app.auth.permissions import PAGE_EVENTS

from app.db import get_db

from app.models import User

from app.services.breeding_sires import (

    delete_sire_classification,

    list_all_sires,

    set_sire_classification,

)
from app.services.stp_report import build_stp_report
from app.services.births_report import build_births_report

from app.services.events_common import EVENT_PAGE_TYPES, build_events_page_report



router = APIRouter(prefix="/api/events")





class SireClassificationBody(BaseModel):

    semen_type: str = Field(..., min_length=1)





def _events_report(

    page_slug: str,

    farm: list[str],

    event_from: dt.date | None,

    event_to: dt.date | None,

    db: Session,

    lact: list[str] | None = None,

    parity: list[str] | None = None,

    fiscal_year: int | None = None,

    disease: str | None = None,

    semen: list[str] | None = None,

    protocol: list[str] | None = None,

    y_min: int | None = None,

    y_max: int | None = None,

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

        fiscal_year=fiscal_year,

        disease=disease,

        semen_types=semen,

        lame_protocols=protocol,

        y_min=y_min,

        y_max=y_max,

    )





@router.get("/calvings")

def api_calvings(

    farm: list[str] = Query(default=[]),

    lact: list[str] = Query(default=[]),

    fiscal_year: int | None = Query(default=None),

    event_from: dt.date | None = Query(default=None),

    event_to: dt.date | None = Query(default=None),

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return _events_report(

        "calvings", farm, event_from, event_to, db, lact=lact or None, fiscal_year=fiscal_year

    )





@router.get("/sales")

def api_sales(

    farm: list[str] = Query(default=[]),

    parity: list[str] = Query(default=[]),

    fiscal_year: int | None = Query(default=None),

    event_from: dt.date | None = Query(default=None),

    event_to: dt.date | None = Query(default=None),

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return _events_report(

        "sales", farm, event_from, event_to, db, parity=parity or None, fiscal_year=fiscal_year

    )





@router.get("/deaths")

def api_deaths(

    farm: list[str] = Query(default=[]),

    parity: list[str] = Query(default=[]),

    fiscal_year: int | None = Query(default=None),

    event_from: dt.date | None = Query(default=None),

    event_to: dt.date | None = Query(default=None),

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return _events_report(

        "deaths", farm, event_from, event_to, db, parity=parity or None, fiscal_year=fiscal_year

    )





@router.get("/disease")

def api_disease(

    farm: list[str] = Query(default=[]),

    parity: list[str] = Query(default=[]),

    disease: str | None = Query(default=None),

    fiscal_year: int | None = Query(default=None),

    event_from: dt.date | None = Query(default=None),

    event_to: dt.date | None = Query(default=None),

    y_min: int | None = Query(default=None),

    y_max: int | None = Query(default=None),

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return _events_report(

        "disease",

        farm,

        event_from,

        event_to,

        db,

        parity=parity or None,

        fiscal_year=fiscal_year,

        disease=disease,

        y_min=y_min,

        y_max=y_max,

    )





@router.get("/hooftrimming")
def api_hooftrimming(
    farm: list[str] = Query(default=[]),
    protocol: list[str] = Query(default=[]),
    fiscal_year: int | None = Query(default=None),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_EVENTS)),
):
    return _events_report(
        "hooftrimming",
        farm,
        event_from,
        event_to,
        db,
        fiscal_year=fiscal_year,
        protocol=protocol or None,
    )





@router.get("/breedings")

def api_breedings(

    farm: list[str] = Query(default=[]),

    parity: list[str] = Query(default=[]),

    semen: list[str] = Query(default=[]),

    fiscal_year: int | None = Query(default=None),

    event_from: dt.date | None = Query(default=None),

    event_to: dt.date | None = Query(default=None),

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return _events_report(

        "breedings",

        farm,

        event_from,

        event_to,

        db,

        parity=parity or None,

        semen=semen or None,

        fiscal_year=fiscal_year,

    )





@router.get("/births")
def api_births(
    farm: list[str] = Query(default=[]),
    category: list[str] = Query(default=[]),
    fiscal_year: int | None = Query(default=None),
    event_from: dt.date | None = Query(default=None),
    event_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_EVENTS)),
):
    return build_births_report(
        db,
        farms=farm or None,
        categories=category or None,
        event_from=event_from,
        event_to=event_to,
        fiscal_year=fiscal_year,
    )


@router.get("/total-protein")
def api_total_protein(
    farm: list[str] = Query(default=[]),
    breed: list[str] = Query(default=[]),
    birth_from: dt.date | None = Query(default=None),
    birth_to: dt.date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_EVENTS)),
):
    return build_stp_report(
        db,
        farms=farm or None,
        breed_types=breed or None,
        birth_from=birth_from,
        birth_to=birth_to,
    )





@router.get("/breedings/sires")

def api_breedings_sires(

    db: Session = Depends(get_db),

    _: User = Depends(require_page(PAGE_EVENTS)),

):

    return list_all_sires(db)





@router.put("/breedings/sires/{sire_code}")

def api_set_breeding_sire(

    sire_code: str,

    body: SireClassificationBody,

    db: Session = Depends(get_db),

    _: User = Depends(require_admin),

):

    try:

        row = set_sire_classification(db, sire_code, body.semen_type)

    except ValueError as exc:

        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return row.to_dict()





@router.delete("/breedings/sires/{sire_code}")

def api_delete_breeding_sire(

    sire_code: str,

    db: Session = Depends(get_db),

    _: User = Depends(require_admin),

):

    if not delete_sire_classification(db, sire_code):

        raise HTTPException(status_code=404, detail="Sire classification not found")

    return {"ok": True}

