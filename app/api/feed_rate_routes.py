"""Feed rate API (Feedlync import + report) and feed contracts."""

from __future__ import annotations

import calendar
import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.permissions import PAGE_FEED_RATE
from app.db import SessionLocal, get_db
from app.models import FeedRateRecord, User
from app.services.feed_contracts import (
    FeedContractError,
    add_feed_option,
    create_feed_contracts_bulk,
    delete_feed_contract,
    feed_contracts_summary,
    get_feed_contract_options,
    list_feed_contracts,
    remove_feed_option,
    update_feed_contract,
)
from app.services.feed_rate_import import (
    get_feed_rate_report,
    get_import_status,
    is_import_running,
    mark_import_started,
    run_import_in_background,
)

router = APIRouter(prefix="/api/feed-rate")


class FeedContractBody(BaseModel):
    purchase_date: dt.date
    product: str = Field(min_length=1, max_length=128)
    product_type: str | None = Field(default=None, max_length=64)
    tonnage: float = Field(ge=0)
    price: float = Field(ge=0)
    supplier: str = Field(min_length=1, max_length=128)
    delivery_months: list[str] = Field(min_length=1)
    delivery_date: dt.date | None = None


class FeedOptionBody(BaseModel):
    value: str = Field(min_length=1, max_length=128)


def _parse_month_start(value: str | None) -> dt.date | None:
    if not value:
        return None
    text = value.strip()
    if len(text) == 7 and text[4] == "-":
        text = text + "-01"
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid month: {value}") from exc


def _parse_month_end(value: str | None) -> dt.date | None:
    start = _parse_month_start(value)
    if start is None:
        return None
    last = calendar.monthrange(start.year, start.month)[1]
    return start.replace(day=last)


@router.get("")
def api_feed_rate_report(
    ration: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return get_feed_rate_report(db, ration=ration or None)


@router.get("/import/status")
def api_feed_rate_import_status(
    _: User = Depends(get_current_user),
):
    return get_import_status()


@router.post("/import")
def api_feed_rate_import(
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_user),
):
    if is_import_running():
        return {"status": "running", "message": "Import already in progress."}

    mark_import_started()
    background_tasks.add_task(run_import_in_background, SessionLocal)
    return {"status": "started", "message": "Feedlync import started."}


@router.get("/status")
def api_feed_rate_status(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    row_count = db.scalar(select(func.count()).select_from(FeedRateRecord)) or 0
    latest_import = db.scalar(select(func.max(FeedRateRecord.import_timestamp)))
    return {
        "row_count": row_count,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "import_status": get_import_status(),
    }


@router.get("/contracts")
def api_list_feed_contracts(
    search: str | None = Query(None),
    supplier: str | None = Query(None),
    product: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return list_feed_contracts(
        db,
        search=search,
        supplier=supplier or None,
        product=product or None,
        date_from=_parse_month_start(date_from),
        date_to=_parse_month_end(date_to),
    )


@router.get("/contracts/summary")
def api_feed_contracts_summary(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return feed_contracts_summary(
        db,
        date_from=_parse_month_start(date_from),
        date_to=_parse_month_start(date_to),
    )


@router.get("/contracts/options")
def api_feed_contract_options(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return get_feed_contract_options(db)


@router.post("/contracts/options/{kind}")
def api_add_feed_option(
    kind: str,
    body: FeedOptionBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    if kind not in ("products", "product_types", "suppliers"):
        raise HTTPException(status_code=400, detail="Invalid option kind.")
    try:
        values = add_feed_option(db, kind, body.value)
    except FeedContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {kind: values, **get_feed_contract_options(db)}


@router.delete("/contracts/options/{kind}")
def api_remove_feed_option(
    kind: str,
    body: FeedOptionBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    if kind not in ("products", "product_types", "suppliers"):
        raise HTTPException(status_code=400, detail="Invalid option kind.")
    try:
        values = remove_feed_option(db, kind, body.value)
    except FeedContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {kind: values, **get_feed_contract_options(db)}


class FeedContractUpdateBody(BaseModel):
    purchase_date: dt.date
    delivery_date: dt.date
    product: str = Field(min_length=1, max_length=128)
    product_type: str | None = Field(default=None, max_length=64)
    tonnage: float = Field(ge=0)
    price: float = Field(ge=0)
    supplier: str = Field(min_length=1, max_length=128)


@router.post("/contracts")
def api_create_feed_contract(
    body: FeedContractBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    try:
        return create_feed_contracts_bulk(db, body.model_dump())
    except FeedContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/contracts/{contract_id}")
def api_update_feed_contract(
    contract_id: int,
    body: FeedContractUpdateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    try:
        return update_feed_contract(db, contract_id, body.model_dump())
    except FeedContractError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/contracts/{contract_id}")
def api_delete_feed_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    try:
        return delete_feed_contract(db, contract_id)
    except FeedContractError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
