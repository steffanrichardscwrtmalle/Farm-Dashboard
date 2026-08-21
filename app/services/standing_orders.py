"""Standing orders for budgeting cash requirements."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import StandingOrder
from app.services.hp_schedules import (
    _normalize_business,
    _normalize_description,
    _normalize_name,
    _normalize_start_month,
    _payment_due_date,
    months_remaining,
)


def _validate_inputs(
    *,
    name: str,
    amount: float,
    months: int,
    payment_day: int,
) -> str:
    name = _normalize_name(name)
    if not name:
        raise ValueError("Standing order name is required.")
    if amount < 0:
        raise ValueError("Payment amount cannot be negative.")
    if months < 1:
        raise ValueError("Months of payments must be at least 1.")
    if payment_day < 1 or payment_day > 31:
        raise ValueError("Payment day must be between 1 and 31.")
    return name


def _serialize(row: StandingOrder, *, as_of: dt.date | None = None) -> dict[str, Any]:
    months = int(row.months)
    amount = float(row.amount or 0)
    remaining = months_remaining(
        start_month=row.start_month,
        months=months,
        payment_day=int(row.payment_day),
        as_of=as_of,
    )
    last_payment = _payment_due_date(
        row.start_month.replace(day=1),
        max(0, months - 1),
        int(row.payment_day),
    )
    return {
        "id": row.id,
        "business": row.business or "CM",
        "name": row.name,
        "description": row.description or "",
        "amount": round(amount, 2),
        "months": months,
        "payment_day": int(row.payment_day),
        "start_month": row.start_month.isoformat(),
        "start_month_label": row.start_month.strftime("%b-%y"),
        "last_payment_date": last_payment.isoformat(),
        "last_payment_label": last_payment.strftime("%b-%y"),
        "sort_order": row.sort_order,
        "is_active": row.is_active,
        "months_remaining": remaining,
        "amount_remaining": round(amount * remaining, 2),
    }


def list_standing_orders(db: Session, *, as_of: dt.date | None = None) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(StandingOrder)
        .where(StandingOrder.is_active.is_(True))
        .order_by(
            StandingOrder.payment_day,
            StandingOrder.business,
            StandingOrder.name,
            StandingOrder.id,
        )
    ).all()
    return [_serialize(row, as_of=as_of) for row in rows]


def create_standing_order(
    db: Session,
    *,
    name: str,
    business: str = "CM",
    description: str = "",
    amount: float,
    months: int,
    payment_day: int,
    start_month: dt.date | str,
    user_id: int | None = None,
) -> dict[str, Any]:
    name = _validate_inputs(
        name=name,
        amount=amount,
        months=months,
        payment_day=payment_day,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    existing = db.scalars(
        select(StandingOrder).where(
            StandingOrder.business == business,
            func.lower(StandingOrder.name) == name.lower(),
        )
    ).first()
    if existing and existing.is_active:
        raise ValueError(f"A standing order named '{name}' already exists for {business}.")
    if existing and not existing.is_active:
        existing.business = business
        existing.name = name
        existing.description = description
        existing.amount = float(amount)
        existing.months = int(months)
        existing.payment_day = int(payment_day)
        existing.start_month = start
        existing.is_active = True
        existing.updated_by_user_id = user_id
        db.commit()
        db.refresh(existing)
        return _serialize(existing)

    max_sort = db.scalar(select(func.max(StandingOrder.sort_order))) or 0
    row = StandingOrder(
        business=business,
        name=name,
        description=description,
        amount=float(amount),
        months=int(months),
        payment_day=int(payment_day),
        start_month=start,
        sort_order=int(max_sort) + 1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"A standing order named '{name}' already exists for {business}."
        ) from exc
    db.refresh(row)
    return _serialize(row)


def update_standing_order(
    db: Session,
    *,
    order_id: int,
    name: str,
    business: str = "CM",
    description: str = "",
    amount: float,
    months: int,
    payment_day: int,
    start_month: dt.date | str,
    user_id: int | None = None,
) -> dict[str, Any]:
    row = db.get(StandingOrder, order_id)
    if not row or not row.is_active:
        raise ValueError("Standing order not found.")

    name = _validate_inputs(
        name=name,
        amount=amount,
        months=months,
        payment_day=payment_day,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    clash = db.scalars(
        select(StandingOrder).where(
            StandingOrder.business == business,
            func.lower(StandingOrder.name) == name.lower(),
            StandingOrder.id != order_id,
            StandingOrder.is_active.is_(True),
        )
    ).first()
    if clash:
        raise ValueError(f"A standing order named '{name}' already exists for {business}.")

    row.business = business
    row.name = name
    row.description = description
    row.amount = float(amount)
    row.months = int(months)
    row.payment_day = int(payment_day)
    row.start_month = start
    row.updated_by_user_id = user_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"A standing order named '{name}' already exists for {business}."
        ) from exc
    db.refresh(row)
    return _serialize(row)


def deactivate_standing_order(db: Session, *, order_id: int) -> None:
    row = db.get(StandingOrder, order_id)
    if not row or not row.is_active:
        raise ValueError("Standing order not found.")
    row.is_active = False
    db.commit()
