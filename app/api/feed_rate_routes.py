"""Feed rate API (Feedlync import + report) and feed contracts."""

from __future__ import annotations

import calendar
import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.permissions import PAGE_FEED_CONTRACTS, PAGE_FEED_RATE
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
from app.services.farm_schedule import normalize_farm
from app.services.feed_usage_settings import (
    list_ingredient_assignments,
    list_ration_assignments,
    save_ingredient_assignment,
    save_ration_assignment,
)
from app.services.feed_usage import (
    XLSX_CONTENT_TYPE as USAGE_XLSX_CONTENT_TYPE,
    build_usage_xlsx,
    get_import_status as get_usage_import_status,
    get_usage_report,
    is_import_running as is_usage_import_running,
    mark_import_started as mark_usage_import_started,
    resolve_usage_month,
    run_usage_import_in_background,
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


class UsageRationAssignmentBody(BaseModel):
    ration_name: str = Field(min_length=1, max_length=255)
    farm: str = ""
    feedlync_ration_id: str | None = Field(default=None, max_length=64)


class UsageIngredientAssignmentBody(BaseModel):
    ingredient_name: str = Field(min_length=1, max_length=255)
    included: bool = True
    feedlync_ingredient_id: str | None = Field(default=None, max_length=64)
    ingredient_type_id: int | None = None
    ingredient_type_name: str | None = Field(default=None, max_length=64)


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


def _usage_month_or_400(value: str | None) -> dt.date:
    try:
        return resolve_usage_month(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _usage_farm_or_400(farm: str | None) -> str:
    try:
        return normalize_farm(farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/usage")
def api_feed_usage_report(
    month: str | None = None,
    farm: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    period = _usage_month_or_400(month)
    farm_key = _usage_farm_or_400(farm)
    return get_usage_report(db, month=period, farm=farm_key)


@router.get("/usage/export.xlsx")
def api_feed_usage_export_xlsx(
    month: str | None = None,
    farm: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    period = _usage_month_or_400(month)
    farm_key = _usage_farm_or_400(farm)
    report = get_usage_report(db, month=period, farm=farm_key)
    filename = f"feed_usage_{farm_key}_{report['month']}.xlsx"
    return Response(
        content=build_usage_xlsx(report),
        media_type=USAGE_XLSX_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/usage/import/status")
def api_feed_usage_import_status(
    _: User = Depends(get_current_user),
):
    return get_usage_import_status()


@router.post("/usage/import")
def api_feed_usage_import(
    background_tasks: BackgroundTasks,
    month: str | None = None,
    _: User = Depends(get_current_user),
):
    period = _usage_month_or_400(month)
    if is_usage_import_running():
        return {"status": "running", "message": "Usage import already in progress."}

    mark_usage_import_started(period.strftime("%Y-%m"))
    background_tasks.add_task(run_usage_import_in_background, SessionLocal, period)
    return {
        "status": "started",
        "message": f"Feedlync usage import started for {period.strftime('%Y-%m')}.",
        "month": period.strftime("%Y-%m"),
    }


@router.get("/usage/settings/rations")
def api_list_usage_ration_assignments(
    sync: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return list_ration_assignments(db, sync=sync)


@router.put("/usage/settings/rations")
def api_save_usage_ration_assignment(
    body: UsageRationAssignmentBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    try:
        row = save_ration_assignment(
            db,
            ration_name=body.ration_name,
            farm=body.farm,
            feedlync_ration_id=body.feedlync_ration_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return row


@router.get("/usage/settings/ingredients")
def api_list_usage_ingredient_assignments(
    sync: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    return list_ingredient_assignments(db, sync=sync)


@router.put("/usage/settings/ingredients")
def api_save_usage_ingredient_assignment(
    body: UsageIngredientAssignmentBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_RATE)),
):
    try:
        row = save_ingredient_assignment(
            db,
            ingredient_name=body.ingredient_name,
            included=body.included,
            feedlync_ingredient_id=body.feedlync_ingredient_id,
            ingredient_type_id=body.ingredient_type_id,
            ingredient_type_name=body.ingredient_type_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return row


@router.get("/contracts")
def api_list_feed_contracts(
    search: str | None = Query(None),
    supplier: str | None = Query(None),
    product: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
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
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
):
    return feed_contracts_summary(
        db,
        date_from=_parse_month_start(date_from),
        date_to=_parse_month_start(date_to),
    )


@router.get("/contracts/options")
def api_feed_contract_options(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
):
    return get_feed_contract_options(db)


@router.post("/contracts/options/{kind}")
def api_add_feed_option(
    kind: str,
    body: FeedOptionBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
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
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
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
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
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
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
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
    _: User = Depends(require_page(PAGE_FEED_CONTRACTS)),
):
    try:
        return delete_feed_contract(db, contract_id)
    except FeedContractError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
