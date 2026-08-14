"""Office Admin API routes."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import (
    ACTION_OFFICE_ADMIN_FALLEN_STOCK,
    ACTION_OFFICE_ADMIN_SALES_PAYMENT,
    PAGE_OFFICE_ADMIN,
)
from app.db import get_db
from app.models import User
from app.services.fallen_stock import (
    confirm_collections,
    list_dest_filter_options as list_fallen_stock_dest_options,
    list_fallen_stock,
    unarchive_collections,
)
from app.services.sales_payments import (
    confirm_payments,
    list_dest_filter_options as list_sales_dest_filter_options,
    list_sales_payments,
    normalize_sales_reasons,
    unarchive_payments,
)
from app.services.stock_accruals import build_stock_accruals_report
from app.services.stock_purchases import list_stock_purchases
from app.services.stock_valuations import build_stock_valuations_report
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/office-admin")


class PaymentKeyItem(BaseModel):
    farm: str
    cow_id: str = ""
    etag: str = ""
    event_date: dt.date


class PaymentBulkBody(BaseModel):
    items: list[PaymentKeyItem] = Field(default_factory=list)


@router.get("/sales-payments")
def api_sales_payments(
    status: str = Query("active", pattern="^(active|archived)$"),
    farm: list[str] | None = Query(None),
    reason: list[str] | None = Query(None),
    dest: str | None = Query(None),
    event_from: dt.date | None = Query(None),
    event_to: dt.date | None = Query(None),
    include_date_bounds: bool = Query(True),
    has_amount: bool | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    return list_sales_payments(
        db,
        status=status,
        farms=farm,
        reasons=normalize_sales_reasons(reason),
        dest=dest,
        event_from=event_from,
        event_to=event_to,
        include_date_bounds=include_date_bounds,
        has_amount=has_amount,
    )


@router.get("/sales-payments/filter-options")
def api_sales_payments_filter_options(
    status: str = Query("active", pattern="^(active|archived)$"),
    farm: list[str] | None = Query(None),
    reason: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    options = list_sales_dest_filter_options(
        db,
        status=status,
        farms=farm,
        reasons=normalize_sales_reasons(reason),
    )
    return {
        "dest_options": options["dest_options"],
        "reason_options": list(normalize_sales_reasons(None)),
        "date_bounds": options["date_bounds"],
    }


@router.post("/sales-payments/confirm")
def api_confirm_sales_payments(
    body: PaymentBulkBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_OFFICE_ADMIN_SALES_PAYMENT)),
):
    items = [item.model_dump() for item in body.items]
    return confirm_payments(db, items, user)


@router.post("/sales-payments/unarchive")
def api_unarchive_sales_payments(
    body: PaymentBulkBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_OFFICE_ADMIN_SALES_PAYMENT)),
):
    items = [item.model_dump() for item in body.items]
    return unarchive_payments(db, items, user)


@router.get("/fallen-stock")
def api_fallen_stock(
    status: str = Query("active", pattern="^(active|archived)$"),
    farm: list[str] | None = Query(None),
    dest: str | None = Query(None),
    event_from: dt.date | None = Query(None),
    event_to: dt.date | None = Query(None),
    include_date_bounds: bool = Query(True),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    return list_fallen_stock(
        db,
        status=status,
        farms=farm,
        dest=dest,
        event_from=event_from,
        event_to=event_to,
        include_date_bounds=include_date_bounds,
    )


@router.get("/fallen-stock/filter-options")
def api_fallen_stock_filter_options(
    status: str = Query("active", pattern="^(active|archived)$"),
    farm: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    options = list_fallen_stock_dest_options(
        db,
        status=status,
        farms=farm,
    )
    return {
        "dest_options": options["dest_options"],
        "date_bounds": options["date_bounds"],
    }


@router.post("/fallen-stock/confirm")
def api_confirm_fallen_stock(
    body: PaymentBulkBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_OFFICE_ADMIN_FALLEN_STOCK)),
):
    items = [item.model_dump() for item in body.items]
    return confirm_collections(db, items, user)


@router.post("/fallen-stock/unarchive")
def api_unarchive_fallen_stock(
    body: PaymentBulkBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_OFFICE_ADMIN_FALLEN_STOCK)),
):
    items = [item.model_dump() for item in body.items]
    return unarchive_collections(db, items, user)


@router.get("/stock-valuations")
def api_stock_valuations(
    farm: list[str] | None = Query(None),
    fiscal_year: int | None = Query(None),
    month_from: dt.date | None = Query(None),
    month_to: dt.date | None = Query(None),
    month: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    return build_stock_valuations_report(
        db,
        farms=farm,
        fiscal_year=fiscal_year,
        month_from=month_from,
        month_to=month_to,
        selected_month=month,
    )


@router.get("/stock-accruals")
def api_stock_accruals(
    farm: list[str] | None = Query(None),
    stock_group: str = Query("cows", pattern="^(cows|youngstock|beef)$"),
    month_from: dt.date | None = Query(None),
    month_to: dt.date | None = Query(None),
    fiscal_year: int | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    return build_stock_accruals_report(
        db,
        farms=farm,
        stock_group=stock_group,
        month_from=month_from,
        month_to=month_to,
        fiscal_year=fiscal_year,
    )


@router.get("/stock-purchases")
def api_stock_purchases(
    farm: list[str] | None = Query(None),
    stock_group: list[str] | None = Query(None),
    month_from: dt.date | None = Query(None),
    month_to: dt.date | None = Query(None),
    fiscal_year: int | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_OFFICE_ADMIN)),
):
    return list_stock_purchases(
        db,
        farms=farm,
        stock_groups=stock_group,
        month_from=month_from,
        month_to=month_to,
        fiscal_year=fiscal_year,
    )
