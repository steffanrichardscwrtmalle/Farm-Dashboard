"""Hire purchase (HP) schedules for benchmarking."""

from __future__ import annotations

import calendar
import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, HpSchedule
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
)

_SEED_PATH = Path(__file__).resolve().parent.parent / "seed_data" / "hp_schedules.json"


def _normalize_name(name: str) -> str:
    return name.strip()


def _normalize_business(business: str | None) -> str:
    value = (business or "").strip().upper()
    if value not in HERD_FARM_OPTIONS:
        raise ValueError(f"Business must be one of: {', '.join(HERD_FARM_OPTIONS)}")
    return value


def _normalize_start_month(value: dt.date | str) -> dt.date:
    if isinstance(value, str):
        raw = value.strip()
        if len(raw) == 7 and raw[4] == "-":
            raw = f"{raw}-01"
        value = dt.date.fromisoformat(raw)
    return value.replace(day=1)


def _normalize_description(description: str | None) -> str:
    return (description or "").strip()


def _validate_inputs(
    *,
    name: str,
    monthly_capital: float,
    monthly_interest: float,
    months: int,
    payment_day: int,
) -> str:
    name = _normalize_name(name)
    if not name:
        raise ValueError("HP name is required.")
    if monthly_capital < 0:
        raise ValueError("Monthly capital cannot be negative.")
    if monthly_interest < 0:
        raise ValueError("Monthly interest cannot be negative.")
    if months < 1:
        raise ValueError("Months of payments must be at least 1.")
    if payment_day < 1 or payment_day > 31:
        raise ValueError("Payment day must be between 1 and 31.")
    return name


def _payment_due_date(start_month: dt.date, installment_index: int, payment_day: int) -> dt.date:
    """Return the due date for installment_index (0-based) of an agreement."""
    month_offset = start_month.month - 1 + installment_index
    year = start_month.year + month_offset // 12
    month = month_offset % 12 + 1
    day = min(payment_day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def months_remaining(
    *,
    start_month: dt.date,
    months: int,
    payment_day: int,
    as_of: dt.date | None = None,
) -> int:
    """Count installments still due on or after as_of (defaults to today)."""
    as_of = as_of or dt.date.today()
    start = start_month.replace(day=1)
    paid = 0
    for i in range(months):
        if _payment_due_date(start, i, payment_day) <= as_of:
            paid += 1
        else:
            break
    return max(0, months - paid)


def _serialize(row: HpSchedule, *, as_of: dt.date | None = None) -> dict[str, Any]:
    months = int(row.months)
    monthly_capital = float(row.monthly_capital or 0)
    monthly_interest = float(row.monthly_interest or 0)
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
        "monthly_capital": round(monthly_capital, 2),
        "monthly_interest": round(monthly_interest, 2),
        "months": months,
        "payment_day": int(row.payment_day),
        "start_month": row.start_month.isoformat(),
        "start_month_label": row.start_month.strftime("%b-%y"),
        "last_payment_date": last_payment.isoformat(),
        "last_payment_label": last_payment.strftime("%b-%y"),
        "sort_order": row.sort_order,
        "is_active": row.is_active,
        "monthly_payment": round(monthly_capital + monthly_interest, 2),
        "total_capital": round(monthly_capital * months, 2),
        "total_interest": round(monthly_interest * months, 2),
        "months_remaining": remaining,
        "capital_remaining": round(monthly_capital * remaining, 2),
        "interest_remaining": round(monthly_interest * remaining, 2),
    }


def list_hp_schedules(db: Session, *, as_of: dt.date | None = None) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(HpSchedule)
        .where(HpSchedule.is_active.is_(True))
        .order_by(
            HpSchedule.payment_day,
            HpSchedule.business,
            HpSchedule.name,
            HpSchedule.id,
        )
    ).all()
    return [_serialize(row, as_of=as_of) for row in rows]


def seed_hp_schedules_if_empty(db: Session) -> int:
    """Load committed HP seed data when the table has no active rows."""
    existing = db.scalars(
        select(HpSchedule).where(HpSchedule.is_active.is_(True)).limit(1)
    ).first()
    if existing is not None:
        return 0
    if not _SEED_PATH.is_file():
        return 0

    try:
        payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, list) or not payload:
        return 0

    added = 0
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        try:
            name = _validate_inputs(
                name=str(item.get("name") or ""),
                monthly_capital=float(item.get("monthly_capital") or 0),
                monthly_interest=float(item.get("monthly_interest") or 0),
                months=int(item.get("months") or 0),
                payment_day=int(item.get("payment_day") or 0),
            )
            business = _normalize_business(str(item.get("business") or "CM"))
            description = _normalize_description(str(item.get("description") or ""))
            start = _normalize_start_month(str(item.get("start_month") or ""))
        except (TypeError, ValueError):
            continue

        existing_row = db.scalars(
            select(HpSchedule).where(
                HpSchedule.business == business,
                func.lower(HpSchedule.name) == name.lower(),
            )
        ).first()
        if existing_row is not None:
            if not existing_row.is_active:
                existing_row.is_active = True
                existing_row.description = description
                existing_row.monthly_capital = float(item["monthly_capital"])
                existing_row.monthly_interest = float(item["monthly_interest"])
                existing_row.months = int(item["months"])
                existing_row.payment_day = int(item["payment_day"])
                existing_row.start_month = start
                added += 1
            continue

        db.add(
            HpSchedule(
                business=business,
                name=name,
                description=description,
                monthly_capital=float(item["monthly_capital"]),
                monthly_interest=float(item["monthly_interest"]),
                months=int(item["months"]),
                payment_day=int(item["payment_day"]),
                start_month=start,
                sort_order=idx + 1,
                is_active=True,
            )
        )
        added += 1

    if added:
        db.commit()
    return added


def get_hp_schedule(
    db: Session, schedule_id: int, *, as_of: dt.date | None = None
) -> dict[str, Any]:
    row = db.get(HpSchedule, schedule_id)
    if not row or not row.is_active:
        raise ValueError("HP schedule not found.")
    return _serialize(row, as_of=as_of)


def create_hp_schedule(
    db: Session,
    *,
    name: str,
    business: str = "CM",
    description: str = "",
    monthly_capital: float,
    monthly_interest: float,
    months: int,
    payment_day: int,
    start_month: dt.date | str,
    user_id: int | None = None,
) -> dict[str, Any]:
    name = _validate_inputs(
        name=name,
        monthly_capital=monthly_capital,
        monthly_interest=monthly_interest,
        months=months,
        payment_day=payment_day,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    existing = db.scalars(
        select(HpSchedule).where(
            HpSchedule.business == business,
            func.lower(HpSchedule.name) == name.lower(),
        )
    ).first()
    if existing and existing.is_active:
        raise ValueError(f"An HP schedule named '{name}' already exists for {business}.")
    if existing and not existing.is_active:
        existing.business = business
        existing.name = name
        existing.description = description
        existing.monthly_capital = float(monthly_capital)
        existing.monthly_interest = float(monthly_interest)
        existing.months = int(months)
        existing.payment_day = int(payment_day)
        existing.start_month = start
        existing.is_active = True
        existing.updated_by_user_id = user_id
        db.commit()
        db.refresh(existing)
        return _serialize(existing)

    max_sort = db.scalar(select(func.max(HpSchedule.sort_order))) or 0
    row = HpSchedule(
        business=business,
        name=name,
        description=description,
        monthly_capital=float(monthly_capital),
        monthly_interest=float(monthly_interest),
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
        raise ValueError(f"An HP schedule named '{name}' already exists for {business}.") from exc
    db.refresh(row)
    return _serialize(row)


def update_hp_schedule(
    db: Session,
    *,
    schedule_id: int,
    name: str,
    business: str = "CM",
    description: str = "",
    monthly_capital: float,
    monthly_interest: float,
    months: int,
    payment_day: int,
    start_month: dt.date | str,
    user_id: int | None = None,
) -> dict[str, Any]:
    row = db.get(HpSchedule, schedule_id)
    if not row or not row.is_active:
        raise ValueError("HP schedule not found.")

    name = _validate_inputs(
        name=name,
        monthly_capital=monthly_capital,
        monthly_interest=monthly_interest,
        months=months,
        payment_day=payment_day,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    clash = db.scalars(
        select(HpSchedule).where(
            HpSchedule.business == business,
            func.lower(HpSchedule.name) == name.lower(),
            HpSchedule.id != schedule_id,
            HpSchedule.is_active.is_(True),
        )
    ).first()
    if clash:
        raise ValueError(f"An HP schedule named '{name}' already exists for {business}.")

    row.business = business
    row.name = name
    row.description = description
    row.monthly_capital = float(monthly_capital)
    row.monthly_interest = float(monthly_interest)
    row.months = int(months)
    row.payment_day = int(payment_day)
    row.start_month = start
    row.updated_by_user_id = user_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"An HP schedule named '{name}' already exists for {business}.") from exc
    db.refresh(row)
    return _serialize(row)

def deactivate_hp_schedule(db: Session, *, schedule_id: int) -> None:
    row = db.get(HpSchedule, schedule_id)
    if not row or not row.is_active:
        raise ValueError("HP schedule not found.")
    row.is_active = False
    db.commit()


def _add_month(month_start: dt.date, months: int = 1) -> dt.date:
    month_start = month_start.replace(day=1)
    offset = month_start.month - 1 + months
    return dt.date(month_start.year + offset // 12, offset % 12 + 1, 1)


def build_hp_payment_index(
    db: Session,
    *,
    fiscal_year: int,
) -> dict[tuple[str, dt.date], dict[str, float]]:
    """Index summed HP payments by (business/farm, month) for a fiscal year."""
    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    rows = db.scalars(select(HpSchedule).where(HpSchedule.is_active.is_(True))).all()

    index: dict[tuple[str, dt.date], dict[str, float]] = {}
    for row in rows:
        months = int(row.months)
        if months < 1:
            continue
        business = (row.business or "CM").strip().upper()
        if business not in HERD_FARM_OPTIONS:
            continue
        start = row.start_month.replace(day=1)
        monthly_capital = float(row.monthly_capital or 0)
        monthly_interest = float(row.monthly_interest or 0)
        payment_day = int(row.payment_day)
        for i in range(months):
            due = _payment_due_date(start, i, payment_day)
            month_key = due.replace(day=1)
            if month_key < fy_start or month_key > fy_end.replace(day=1):
                continue
            cell = index.setdefault(
                (business, month_key),
                {"monthly_capital": 0.0, "monthly_interest": 0.0, "monthly_payment": 0.0},
            )
            cell["monthly_capital"] += monthly_capital
            cell["monthly_interest"] += monthly_interest
            cell["monthly_payment"] += monthly_capital + monthly_interest

    for cell in index.values():
        cell["monthly_capital"] = round(cell["monthly_capital"], 2)
        cell["monthly_interest"] = round(cell["monthly_interest"], 2)
        cell["monthly_payment"] = round(cell["monthly_payment"], 2)
    return index


def build_hp_payment_chart(
    db: Session,
    *,
    as_of: dt.date | None = None,
    business: str | None = None,
    from_month: dt.date | str | None = None,
    to_month: dt.date | str | None = None,
) -> dict[str, Any]:
    """Sum monthly HP payments from current fiscal-year start into the future."""
    as_of = as_of or dt.date.today()
    fiscal_year = _fiscal_year_from_date(as_of)
    fy_start, _fy_end = _fiscal_year_calendar_bounds(fiscal_year)

    business_filter = None
    if business and str(business).strip():
        business_filter = _normalize_business(business)

    query = select(HpSchedule).where(HpSchedule.is_active.is_(True))
    if business_filter:
        query = query.where(HpSchedule.business == business_filter)
    rows = db.scalars(query).all()

    capital_by_month: dict[dt.date, float] = {}
    interest_by_month: dict[dt.date, float] = {}
    data_max = fy_start

    for row in rows:
        months = int(row.months)
        if months < 1:
            continue
        start = row.start_month.replace(day=1)
        monthly_capital = float(row.monthly_capital or 0)
        monthly_interest = float(row.monthly_interest or 0)
        payment_day = int(row.payment_day)
        for i in range(months):
            due = _payment_due_date(start, i, payment_day)
            month_key = due.replace(day=1)
            if month_key < fy_start:
                continue
            capital_by_month[month_key] = capital_by_month.get(month_key, 0.0) + monthly_capital
            interest_by_month[month_key] = interest_by_month.get(month_key, 0.0) + monthly_interest
            if month_key > data_max:
                data_max = month_key

    if not capital_by_month and not interest_by_month:
        data_max = fy_start

    available_from = fy_start
    available_to = data_max

    range_from = available_from
    range_to = available_to
    if from_month is not None and str(from_month).strip():
        range_from = _normalize_start_month(from_month)
    if to_month is not None and str(to_month).strip():
        range_to = _normalize_start_month(to_month)
    if range_from < available_from:
        range_from = available_from
    if range_to > available_to:
        range_to = available_to
    if range_to < range_from:
        range_to = range_from

    months_out: list[dict[str, Any]] = []
    cursor = range_from
    while cursor <= range_to:
        capital = round(capital_by_month.get(cursor, 0.0), 2)
        interest = round(interest_by_month.get(cursor, 0.0), 2)
        months_out.append(
            {
                "month": cursor.isoformat(),
                "month_label": cursor.strftime("%b-%y"),
                "capital": capital,
                "interest": interest,
                "total": round(capital + interest, 2),
            }
        )
        cursor = _add_month(cursor, 1)

    return {
        "fiscal_year": fiscal_year,
        "business": business_filter,
        "from_month": range_from.isoformat(),
        "to_month": range_to.isoformat(),
        "available_from": available_from.isoformat(),
        "available_to": available_to.isoformat(),
        "months": months_out,
        "totals": {
            "capital": round(sum(m["capital"] for m in months_out), 2),
            "interest": round(sum(m["interest"] for m in months_out), 2),
            "total": round(sum(m["total"] for m in months_out), 2),
        },
    }
