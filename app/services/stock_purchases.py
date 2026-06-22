"""Derived stock purchase listing for Office Admin."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import PURCHASE_STOCK_GROUP_OPTIONS, STOCK_GROUP_OPTIONS, StockPurchaseAnimal
from app.services.events_common import normalize_farms

VALID_STOCK_GROUPS = set(STOCK_GROUP_OPTIONS)
VALID_PURCHASE_GROUPS = set(PURCHASE_STOCK_GROUP_OPTIONS)


def normalize_stock_group(value: str | None) -> str:
    normalized = (value or STOCK_GROUP_OPTIONS[0]).strip().lower()
    if normalized not in VALID_STOCK_GROUPS:
        return STOCK_GROUP_OPTIONS[0]
    return normalized


def normalize_purchase_stock_group(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_PURCHASE_GROUPS:
        return None
    return normalized


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def list_stock_purchases(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_groups: list[str] | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_groups = [
        group
        for group in (
            normalize_purchase_stock_group(value) for value in (stock_groups or [])
        )
        if group
    ]

    query = select(StockPurchaseAnimal).order_by(
        StockPurchaseAnimal.edat.desc(),
        StockPurchaseAnimal.farm.asc(),
        StockPurchaseAnimal.etag.asc(),
    )
    if selected_farms:
        query = query.where(StockPurchaseAnimal.farm.in_(selected_farms))
    if selected_groups:
        query = query.where(StockPurchaseAnimal.stock_group.in_(selected_groups))
    if month_from is not None:
        query = query.where(StockPurchaseAnimal.edat >= _month_start(month_from))
    if month_to is not None:
        query = query.where(StockPurchaseAnimal.edat <= month_to)

    records = list(db.scalars(query).all())
    latest_import = db.scalar(select(func.max(StockPurchaseAnimal.import_timestamp)))

    summary_query = (
        select(
            StockPurchaseAnimal.farm,
            StockPurchaseAnimal.stock_group,
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
            func.count(),
        )
        .group_by(
            StockPurchaseAnimal.farm,
            StockPurchaseAnimal.stock_group,
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
        )
        .order_by(
            extract("year", StockPurchaseAnimal.edat).desc(),
            extract("month", StockPurchaseAnimal.edat).desc(),
            StockPurchaseAnimal.farm.asc(),
        )
    )
    if selected_farms:
        summary_query = summary_query.where(StockPurchaseAnimal.farm.in_(selected_farms))
    if selected_groups:
        summary_query = summary_query.where(
            StockPurchaseAnimal.stock_group.in_(selected_groups)
        )
    if month_from is not None:
        summary_query = summary_query.where(
            StockPurchaseAnimal.edat >= _month_start(month_from)
        )
    if month_to is not None:
        summary_query = summary_query.where(StockPurchaseAnimal.edat <= month_to)

    summary_rows: list[dict[str, Any]] = []
    for farm, group, year, month, count in db.execute(summary_query).all():
        if year is None or month is None:
            continue
        month_start = dt.date(int(year), int(month), 1)
        summary_rows.append(
            {
                "farm": str(farm),
                "stock_group": str(group),
                "month_start": month_start.isoformat(),
                "quantity": int(count),
            }
        )

    return {
        "rows": [record.to_dict() for record in records],
        "total": len(records),
        "summary_rows": summary_rows,
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
