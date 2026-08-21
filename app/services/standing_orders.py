"""Standing orders for budgeting cash requirements."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StandingOrder
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
)
from app.services.hp_schedules import (
    _add_month,
    _normalize_business,
    _normalize_description,
    _normalize_name,
    _normalize_start_month,
    _payment_due_date,
    _roll_weekend_to_monday,
)

FREQUENCY_MONTHLY = "monthly"
FREQUENCY_OTHER = "other"
_FREQUENCIES = {FREQUENCY_MONTHLY, FREQUENCY_OTHER}


def _normalize_frequency(frequency: str | None) -> str:
    value = (frequency or FREQUENCY_MONTHLY).strip().lower()
    if value not in _FREQUENCIES:
        raise ValueError("Repeat must be monthly or other.")
    return value


def _normalize_interval_days(frequency: str, interval_days: int | None) -> int | None:
    if frequency != FREQUENCY_OTHER:
        return None
    try:
        days = int(interval_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days < 1:
        raise ValueError("How often it recurs must be at least 1 day.")
    if days > 365:
        raise ValueError("How often it recurs cannot be more than 365 days.")
    return days


def _validate_inputs(
    *,
    name: str,
    amount: float,
    months: int,
    payment_day: int,
    frequency: str | None = None,
    interval_days: int | None = None,
) -> tuple[str, str, int | None]:
    name = _normalize_name(name)
    if not name:
        raise ValueError("Standing order name is required.")
    if amount < 0:
        raise ValueError("Payment amount cannot be negative.")
    if months < 1:
        raise ValueError("Total payments must be at least 1.")
    if payment_day < 1 or payment_day > 31:
        raise ValueError("1st payment day must be between 1 and 31.")
    frequency = _normalize_frequency(frequency)
    interval = _normalize_interval_days(frequency, interval_days)
    return name, frequency, interval


def _first_unrolled_date(start_month: dt.date, payment_day: int) -> dt.date:
    start = start_month.replace(day=1)
    day = min(int(payment_day), calendar.monthrange(start.year, start.month)[1])
    return dt.date(start.year, start.month, day)


def iter_standing_order_due_dates(
    *,
    start_month: dt.date,
    months: int,
    payment_day: int,
    frequency: str | None = None,
    interval_days: int | None = None,
) -> list[dt.date]:
    """Due dates for a standing order, with weekend dues rolled to Monday.

    ``months`` is the total number of payments for every frequency.
    """
    start = start_month.replace(day=1)
    count = max(0, int(months))
    freq = _normalize_frequency(frequency)
    if freq != FREQUENCY_OTHER:
        return [_payment_due_date(start, i, int(payment_day)) for i in range(count)]

    days = int(interval_days or 0)
    if days < 1 or count < 1:
        return []
    cursor = _first_unrolled_date(start, int(payment_day))
    return [
        _roll_weekend_to_monday(cursor + dt.timedelta(days=days * i))
        for i in range(count)
    ]


def _frequency_label(frequency: str, interval_days: int | None) -> str:
    if frequency == FREQUENCY_OTHER and interval_days:
        unit = "day" if int(interval_days) == 1 else "days"
        return f"Every {int(interval_days)} {unit}"
    return "Monthly"


def _serialize(row: StandingOrder, *, as_of: dt.date | None = None) -> dict[str, Any]:
    months = int(row.months)
    amount = float(row.amount or 0)
    frequency = _normalize_frequency(getattr(row, "frequency", None))
    interval_days = getattr(row, "interval_days", None)
    dues = iter_standing_order_due_dates(
        start_month=row.start_month,
        months=months,
        payment_day=int(row.payment_day),
        frequency=frequency,
        interval_days=interval_days,
    )
    as_of_date = as_of or dt.date.today()
    remaining_dues = [due for due in dues if due > as_of_date]
    remaining = len(remaining_dues)
    next_payment = remaining_dues[0] if remaining_dues else None
    last_payment = dues[-1] if dues else _payment_due_date(
        row.start_month.replace(day=1),
        max(0, months - 1),
        int(row.payment_day),
    )
    last_label = (
        last_payment.strftime("%d %b %y")
        if frequency == FREQUENCY_OTHER
        else last_payment.strftime("%b-%y")
    )
    return {
        "id": row.id,
        "business": row.business or "CM",
        "name": row.name,
        "description": row.description or "",
        "amount": round(amount, 2),
        "months": months,
        "total_payments": months,
        "frequency": frequency,
        "interval_days": int(interval_days) if frequency == FREQUENCY_OTHER and interval_days else None,
        "frequency_label": _frequency_label(frequency, interval_days),
        "payment_day": int(row.payment_day),
        "start_month": row.start_month.isoformat(),
        "start_month_label": row.start_month.strftime("%b-%y"),
        "next_payment_date": next_payment.isoformat() if next_payment else None,
        "next_payment_label": next_payment.strftime("%d %b %y") if next_payment else "—",
        "last_payment_date": last_payment.isoformat(),
        "last_payment_label": last_label,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
        "months_remaining": remaining,
        "payments_remaining": remaining,
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
    frequency: str = FREQUENCY_MONTHLY,
    interval_days: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    name, frequency, interval = _validate_inputs(
        name=name,
        amount=amount,
        months=months,
        payment_day=payment_day,
        frequency=frequency,
        interval_days=interval_days,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    max_sort = db.scalar(select(func.max(StandingOrder.sort_order))) or 0
    row = StandingOrder(
        business=business,
        name=name,
        description=description,
        amount=float(amount),
        months=int(months),
        frequency=frequency,
        interval_days=interval,
        payment_day=int(payment_day),
        start_month=start,
        sort_order=int(max_sort) + 1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    db.add(row)
    db.commit()
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
    frequency: str = FREQUENCY_MONTHLY,
    interval_days: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    row = db.get(StandingOrder, order_id)
    if not row or not row.is_active:
        raise ValueError("Standing order not found.")

    name, frequency, interval = _validate_inputs(
        name=name,
        amount=amount,
        months=months,
        payment_day=payment_day,
        frequency=frequency,
        interval_days=interval_days,
    )
    business = _normalize_business(business)
    description = _normalize_description(description)
    start = _normalize_start_month(start_month)

    row.business = business
    row.name = name
    row.description = description
    row.amount = float(amount)
    row.months = int(months)
    row.frequency = frequency
    row.interval_days = interval
    row.payment_day = int(payment_day)
    row.start_month = start
    row.updated_by_user_id = user_id
    db.commit()
    db.refresh(row)
    return _serialize(row)


def deactivate_standing_order(db: Session, *, order_id: int) -> None:
    row = db.get(StandingOrder, order_id)
    if not row or not row.is_active:
        raise ValueError("Standing order not found.")
    row.is_active = False
    db.commit()


def build_standing_order_payment_chart(
    db: Session,
    *,
    as_of: dt.date | None = None,
    business: str | None = None,
    from_month: dt.date | str | None = None,
    to_month: dt.date | str | None = None,
) -> dict[str, Any]:
    """Sum standing-order payments by month from the current fiscal-year start."""
    as_of = as_of or dt.date.today()
    fiscal_year = _fiscal_year_from_date(as_of)
    fy_start, _fy_end = _fiscal_year_calendar_bounds(fiscal_year)

    business_filter = None
    if business and str(business).strip():
        business_filter = _normalize_business(business)

    query = select(StandingOrder).where(StandingOrder.is_active.is_(True))
    if business_filter:
        query = query.where(StandingOrder.business == business_filter)
    rows = db.scalars(query).all()

    amount_by_month: dict[dt.date, float] = {}
    count_by_month: dict[dt.date, int] = {}
    data_max = fy_start

    for row in rows:
        months = int(row.months)
        if months < 1:
            continue
        amount = float(row.amount or 0)
        dues = iter_standing_order_due_dates(
            start_month=row.start_month,
            months=months,
            payment_day=int(row.payment_day),
            frequency=getattr(row, "frequency", None),
            interval_days=getattr(row, "interval_days", None),
        )
        for due in dues:
            month_key = due.replace(day=1)
            if month_key < fy_start:
                continue
            amount_by_month[month_key] = amount_by_month.get(month_key, 0.0) + amount
            count_by_month[month_key] = count_by_month.get(month_key, 0) + 1
            if month_key > data_max:
                data_max = month_key

    if not amount_by_month:
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
        total = round(amount_by_month.get(cursor, 0.0), 2)
        months_out.append(
            {
                "month": cursor.isoformat(),
                "month_label": cursor.strftime("%b-%y"),
                "total": total,
                "payment_count": int(count_by_month.get(cursor, 0)),
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
            "total": round(sum(m["total"] for m in months_out), 2),
            "payment_count": sum(m["payment_count"] for m in months_out),
        },
    }
