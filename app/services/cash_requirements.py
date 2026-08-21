"""Cash requirements: dated HP, standing order, and rent payments for the month and FY chart."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    HERD_FARM_OPTIONS,
    HpSchedule,
    RentalAgreement,
    RentalAgreementPayment,
    StandingOrder,
)
from app.services.benchmarking import fiscal_year_months
from app.services.events_common import _fiscal_year_from_date
from app.services.hp_schedules import _normalize_business, _payment_due_date
from app.services.rental_agreements import rent_payment_due_date


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _normalize_month(value: dt.date | str | None, *, today: dt.date) -> dt.date:
    if value is None:
        return _month_start(today)
    if isinstance(value, str):
        raw = value.strip()
        if len(raw) == 7 and raw[4] == "-":
            raw = f"{raw}-01"
        value = dt.date.fromisoformat(raw)
    return _month_start(value)


def _round_money(value: float) -> float:
    return round(value, 2)


def _is_paid(due_date: dt.date, today: dt.date) -> bool:
    return due_date <= today


def _payment_row(
    *,
    source: str,
    source_id: int,
    business: str,
    name: str,
    due_date: dt.date,
    amount: float,
    today: dt.date,
) -> dict[str, Any]:
    paid = _is_paid(due_date, today)
    return {
        "source": source,
        "source_id": source_id,
        "business": business,
        "name": name,
        "due_date": due_date.isoformat(),
        "due_label": due_date.strftime("%d %b %Y"),
        "amount": _round_money(amount),
        "paid": paid,
        "status": "paid" if paid else "due",
    }


def _hp_payments(
    db: Session,
    *,
    month_starts: list[dt.date],
    business: str | None,
    today: dt.date,
) -> list[dict[str, Any]]:
    month_set = set(month_starts)
    if not month_set:
        return []
    query = select(HpSchedule).where(HpSchedule.is_active.is_(True))
    if business:
        query = query.where(HpSchedule.business == business)
    rows = db.scalars(query).all()

    payments: list[dict[str, Any]] = []
    for row in rows:
        months = int(row.months)
        if months < 1:
            continue
        farm = (row.business or "CM").strip().upper()
        if farm not in HERD_FARM_OPTIONS:
            continue
        start = row.start_month.replace(day=1)
        amount = float(row.monthly_capital or 0) + float(row.monthly_interest or 0)
        if amount <= 0:
            continue
        payment_day = int(row.payment_day)
        for i in range(months):
            due = _payment_due_date(start, i, payment_day)
            if due.replace(day=1) not in month_set:
                continue
            payments.append(
                _payment_row(
                    source="hp",
                    source_id=row.id,
                    business=farm,
                    name=row.name,
                    due_date=due,
                    amount=amount,
                    today=today,
                )
            )
    return payments


def _standing_order_payments(
    db: Session,
    *,
    month_starts: list[dt.date],
    business: str | None,
    today: dt.date,
) -> list[dict[str, Any]]:
    month_set = set(month_starts)
    if not month_set:
        return []
    query = select(StandingOrder).where(StandingOrder.is_active.is_(True))
    if business:
        query = query.where(StandingOrder.business == business)
    rows = db.scalars(query).all()

    payments: list[dict[str, Any]] = []
    for row in rows:
        months = int(row.months)
        if months < 1:
            continue
        farm = (row.business or "CM").strip().upper()
        if farm not in HERD_FARM_OPTIONS:
            continue
        start = row.start_month.replace(day=1)
        amount = float(row.amount or 0)
        if amount <= 0:
            continue
        payment_day = int(row.payment_day)
        for i in range(months):
            due = _payment_due_date(start, i, payment_day)
            if due.replace(day=1) not in month_set:
                continue
            payments.append(
                _payment_row(
                    source="standing_order",
                    source_id=row.id,
                    business=farm,
                    name=row.name,
                    due_date=due,
                    amount=amount,
                    today=today,
                )
            )
    return payments


def _rent_payments(
    db: Session,
    *,
    month_starts: list[dt.date],
    business: str | None,
    today: dt.date,
) -> list[dict[str, Any]]:
    if not month_starts:
        return []
    query = (
        select(RentalAgreementPayment)
        .join(RentalAgreement)
        .options(selectinload(RentalAgreementPayment.agreement))
        .where(
            RentalAgreement.is_active.is_(True),
            RentalAgreementPayment.payment_month.in_(month_starts),
            RentalAgreementPayment.amount.isnot(None),
        )
    )
    if business:
        query = query.where(RentalAgreement.business == business)
    rows = db.scalars(query).all()

    payments: list[dict[str, Any]] = []
    for row in rows:
        amount = float(row.amount or 0)
        if amount <= 0:
            continue
        agreement = row.agreement
        farm = (agreement.business or "CM").strip().upper()
        if farm not in HERD_FARM_OPTIONS:
            continue
        due = rent_payment_due_date(row.payment_month, agreement.payment_day)
        payments.append(
            _payment_row(
                source="rent",
                source_id=agreement.id,
                business=farm,
                name=agreement.farm_name,
                due_date=due,
                amount=amount,
                today=today,
            )
        )
    return payments


def _sort_payments(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        payments,
        key=lambda row: (row["due_date"], row["source"], row["name"].lower()),
    )


def _summarise(payments: list[dict[str, Any]]) -> dict[str, float]:
    total = 0.0
    paid = 0.0
    remaining = 0.0
    for row in payments:
        amount = float(row["amount"])
        total += amount
        if row["paid"]:
            paid += amount
        else:
            remaining += amount
    return {
        "total": _round_money(total),
        "paid": _round_money(paid),
        "remaining": _round_money(remaining),
    }


def _chart_months(
    *,
    month_starts: list[dt.date],
    payments: list[dict[str, Any]],
    today: dt.date,
) -> list[dict[str, Any]]:
    current_month = _month_start(today)
    buckets: dict[str, dict[str, float]] = {
        month.isoformat(): {"total": 0.0, "paid": 0.0, "remaining": 0.0}
        for month in month_starts
    }
    for row in payments:
        month_key = row["due_date"][:7] + "-01"
        bucket = buckets.get(month_key)
        if bucket is None:
            continue
        amount = float(row["amount"])
        bucket["total"] += amount
        if row["paid"]:
            bucket["paid"] += amount
        else:
            bucket["remaining"] += amount

    return [
        {
            "month": month.isoformat(),
            "month_label": month.strftime("%b-%y"),
            "is_current": month == current_month,
            "total": _round_money(buckets[month.isoformat()]["total"]),
            "paid": _round_money(buckets[month.isoformat()]["paid"]),
            "remaining": _round_money(buckets[month.isoformat()]["remaining"]),
        }
        for month in month_starts
    ]


def build_cash_requirements_report(
    db: Session,
    *,
    business: str | None = None,
    month: dt.date | str | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Dated HP, standing-order, and rent outflows for a month, plus FY monthly requirement."""
    as_of = today or dt.date.today()
    selected_month = _normalize_month(month, today=as_of)
    fiscal_year = _fiscal_year_from_date(selected_month)
    months = fiscal_year_months(fiscal_year)

    business_filter = None
    if business and str(business).strip():
        business_filter = _normalize_business(business)

    all_payments = _sort_payments(
        _hp_payments(
            db, month_starts=months, business=business_filter, today=as_of
        )
        + _standing_order_payments(
            db, month_starts=months, business=business_filter, today=as_of
        )
        + _rent_payments(
            db, month_starts=months, business=business_filter, today=as_of
        )
    )
    selected_iso = selected_month.isoformat()
    month_payments = [
        row for row in all_payments if row["due_date"][:7] + "-01" == selected_iso
    ]
    month_totals = _summarise(month_payments)
    chart_months = _chart_months(
        month_starts=months, payments=all_payments, today=as_of
    )

    return {
        "today": as_of.isoformat(),
        "fiscal_year": fiscal_year,
        "business": business_filter,
        "month": selected_iso,
        "month_label": selected_month.strftime("%b-%y"),
        "payments": month_payments,
        "totals": month_totals,
        "months": chart_months,
        "year_totals": _summarise(all_payments),
        "farm_options": list(HERD_FARM_OPTIONS),
    }
