"""Manual stock purchase entries for Office Admin stock accruals."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import STOCK_GROUP_OPTIONS, StockPurchaseRecord, User
from app.services.events_common import normalize_farms

VALID_STOCK_GROUPS = set(STOCK_GROUP_OPTIONS)


def normalize_stock_group(value: str | None) -> str:
    normalized = (value or STOCK_GROUP_OPTIONS[0]).strip().lower()
    if normalized not in VALID_STOCK_GROUPS:
        return STOCK_GROUP_OPTIONS[0]
    return normalized


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def list_stock_purchases(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_group: str | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    query = select(StockPurchaseRecord).order_by(
        StockPurchaseRecord.month_start.desc(),
        StockPurchaseRecord.farm.asc(),
    )
    if selected_farms:
        query = query.where(StockPurchaseRecord.farm.in_(selected_farms))
    if stock_group:
        query = query.where(StockPurchaseRecord.stock_group == normalize_stock_group(stock_group))
    if month_from is not None:
        query = query.where(StockPurchaseRecord.month_start >= _month_start(month_from))
    if month_to is not None:
        query = query.where(StockPurchaseRecord.month_start <= _month_start(month_to))

    records = list(db.scalars(query).all())
    return {"rows": [record.to_dict() for record in records], "total": len(records)}


def upsert_stock_purchase(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_start: dt.date,
    quantity: int,
    notes: str | None,
    user: User,
) -> dict[str, Any]:
    farm = farm.strip().upper()
    if farm not in normalize_farms([farm]):
        raise ValueError(f"Invalid farm: {farm}")
    stock_group = normalize_stock_group(stock_group)
    month = _month_start(month_start)
    if quantity < 0:
        raise ValueError("Quantity cannot be negative")

    record = db.scalar(
        select(StockPurchaseRecord).where(
            StockPurchaseRecord.farm == farm,
            StockPurchaseRecord.stock_group == stock_group,
            StockPurchaseRecord.month_start == month,
        )
    )
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    if record:
        record.quantity = quantity
        record.notes = (notes or "").strip() or None
        record.created_by_user_id = user.id
        record.updated_at = now
    else:
        record = StockPurchaseRecord(
            farm=farm,
            stock_group=stock_group,
            month_start=month,
            quantity=quantity,
            notes=(notes or "").strip() or None,
            created_by_user_id=user.id,
            updated_at=now,
        )
        db.add(record)

    db.commit()
    db.refresh(record)
    return record.to_dict()


def delete_stock_purchase(db: Session, record_id: int) -> bool:
    record = db.get(StockPurchaseRecord, record_id)
    if record is None:
        return False
    db.delete(record)
    db.commit()
    return True
