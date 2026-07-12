"""Land rental agreements and monthly rent schedules for benchmarking."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, RentalAgreement, RentalAgreementPayment
from app.services.benchmarking import available_fiscal_years, fiscal_year_months


def _normalize_business(business: str) -> str:
    normalized = business.strip().upper()
    if normalized not in HERD_FARM_OPTIONS:
        raise ValueError(f"Business must be one of: {', '.join(HERD_FARM_OPTIONS)}")
    return normalized


def _normalize_farm_name(farm_name: str) -> str:
    normalized = farm_name.strip()
    if not normalized:
        raise ValueError("Farm name is required")
    return normalized


def _normalize_farm_size(farm_size: float) -> float:
    try:
        value = float(farm_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("Farm size must be a number") from exc
    if value < 0:
        raise ValueError("Farm size cannot be negative")
    return value


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _round_money(value: float) -> float:
    return round(value)


def _per_acre(total: float | None, farm_size: float | None) -> float | None:
    if total is None or farm_size is None or farm_size <= 0:
        return None
    return round(total / farm_size, 2)


def _serialize_agreement(
    agreement: RentalAgreement,
    *,
    months: list[dt.date],
    amounts_by_month: dict[dt.date, float],
) -> dict[str, Any]:
    amounts: dict[str, float | None] = {}
    total = 0.0
    has_amount = False
    for month in months:
        value = amounts_by_month.get(month)
        amounts[month.isoformat()] = value
        if value is not None:
            total += value
            has_amount = True
    year_total = _round_money(total) if has_amount else None
    return {
        "id": agreement.id,
        "business": agreement.business,
        "farm_name": agreement.farm_name,
        "farm_size": agreement.farm_size,
        "sort_order": agreement.sort_order,
        "amounts": amounts,
        "total": year_total,
        "per_acre": _per_acre(year_total, agreement.farm_size),
    }


def _empty_month_totals(months: list[dt.date]) -> dict[str, float | None]:
    return {month.isoformat(): None for month in months}


def _sum_month_maps(
    maps: list[dict[str, float | None]],
    months: list[dt.date],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for month in months:
        key = month.isoformat()
        values = [m[key] for m in maps if m.get(key) is not None]
        result[key] = _round_money(sum(values)) if values else None
    return result


def _totals_payload(
    amounts: dict[str, float | None],
    months: list[dt.date],
) -> dict[str, Any]:
    values = [amounts[month.isoformat()] for month in months if amounts.get(month.isoformat()) is not None]
    total = _round_money(sum(values)) if values else None
    return {"amounts": amounts, "total": total}


def list_rental_agreements(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(RentalAgreement)
        .where(RentalAgreement.is_active.is_(True))
        .order_by(
            RentalAgreement.business,
            RentalAgreement.sort_order,
            RentalAgreement.farm_name,
        )
    ).all()
    return [
        {
            "id": row.id,
            "business": row.business,
            "farm_name": row.farm_name,
            "farm_size": row.farm_size,
            "sort_order": row.sort_order,
        }
        for row in rows
    ]


def create_rental_agreement(
    db: Session,
    *,
    business: str,
    farm_name: str,
    farm_size: float,
    user_id: int | None = None,
) -> dict[str, Any]:
    business = _normalize_business(business)
    farm_name = _normalize_farm_name(farm_name)
    farm_size = _normalize_farm_size(farm_size)

    existing = db.scalars(
        select(RentalAgreement).where(
            RentalAgreement.business == business,
            func.lower(RentalAgreement.farm_name) == farm_name.lower(),
        )
    ).first()
    if existing is not None:
        if existing.is_active:
            raise ValueError(f"A rental agreement for '{farm_name}' already exists on {business}")
        existing.is_active = True
        existing.farm_name = farm_name
        existing.farm_size = farm_size
        existing.updated_by_user_id = user_id
        db.commit()
        db.refresh(existing)
        return {
            "id": existing.id,
            "business": existing.business,
            "farm_name": existing.farm_name,
            "farm_size": existing.farm_size,
            "sort_order": existing.sort_order,
        }

    max_sort = db.scalar(select(func.max(RentalAgreement.sort_order))) or 0
    agreement = RentalAgreement(
        business=business,
        farm_name=farm_name,
        farm_size=farm_size,
        sort_order=int(max_sort) + 1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    db.add(agreement)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A rental agreement for '{farm_name}' already exists on {business}") from exc
    db.refresh(agreement)
    return {
        "id": agreement.id,
        "business": agreement.business,
        "farm_name": agreement.farm_name,
        "farm_size": agreement.farm_size,
        "sort_order": agreement.sort_order,
    }


def update_rental_agreement(
    db: Session,
    *,
    agreement_id: int,
    business: str,
    farm_name: str,
    farm_size: float,
    user_id: int | None = None,
) -> dict[str, Any]:
    agreement = db.get(RentalAgreement, agreement_id)
    if agreement is None or not agreement.is_active:
        raise ValueError("Rental agreement not found")

    business = _normalize_business(business)
    farm_name = _normalize_farm_name(farm_name)
    farm_size = _normalize_farm_size(farm_size)

    clash = db.scalars(
        select(RentalAgreement).where(
            RentalAgreement.business == business,
            func.lower(RentalAgreement.farm_name) == farm_name.lower(),
            RentalAgreement.id != agreement_id,
            RentalAgreement.is_active.is_(True),
        )
    ).first()
    if clash is not None:
        raise ValueError(f"A rental agreement for '{farm_name}' already exists on {business}")

    agreement.business = business
    agreement.farm_name = farm_name
    agreement.farm_size = farm_size
    agreement.updated_by_user_id = user_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A rental agreement for '{farm_name}' already exists on {business}") from exc
    db.refresh(agreement)
    return {
        "id": agreement.id,
        "business": agreement.business,
        "farm_name": agreement.farm_name,
        "farm_size": agreement.farm_size,
        "sort_order": agreement.sort_order,
    }


def deactivate_rental_agreement(db: Session, *, agreement_id: int) -> None:
    agreement = db.get(RentalAgreement, agreement_id)
    if agreement is None or not agreement.is_active:
        raise ValueError("Rental agreement not found")
    agreement.is_active = False
    db.commit()


def save_rental_payments(
    db: Session,
    *,
    fiscal_year: int,
    rows: list[dict[str, Any]],
    user_id: int | None = None,
) -> dict[str, int]:
    """Upsert monthly rent amounts for the given fiscal year."""
    months = set(fiscal_year_months(fiscal_year))
    updated = 0
    cleared = 0

    for row in rows:
        agreement_id = int(row["agreement_id"])
        agreement = db.get(RentalAgreement, agreement_id)
        if agreement is None or not agreement.is_active:
            raise ValueError(f"Rental agreement {agreement_id} not found")

        payment_month = row["payment_month"]
        if isinstance(payment_month, str):
            payment_month = dt.date.fromisoformat(payment_month)
        payment_month = _month_start(payment_month)
        if payment_month not in months:
            raise ValueError(
                f"Payment month {payment_month.isoformat()} is outside fiscal year {fiscal_year}"
            )

        raw_amount = row.get("amount")
        existing = db.scalars(
            select(RentalAgreementPayment).where(
                RentalAgreementPayment.agreement_id == agreement_id,
                RentalAgreementPayment.payment_month == payment_month,
            )
        ).first()

        if raw_amount is None or raw_amount == "":
            if existing is not None:
                db.delete(existing)
                cleared += 1
            continue

        amount = float(raw_amount)
        if amount < 0:
            raise ValueError("Rent amount cannot be negative")
        amount = _round_money(amount)

        if existing is None:
            db.add(
                RentalAgreementPayment(
                    agreement_id=agreement_id,
                    payment_month=payment_month,
                    amount=amount,
                    updated_by_user_id=user_id,
                )
            )
        else:
            existing.amount = amount
            existing.updated_by_user_id = user_id
        updated += 1

    db.commit()
    return {"updated": updated, "cleared": cleared}


def build_rental_agreements_report(
    db: Session,
    *,
    fiscal_year: int,
) -> dict[str, Any]:
    months = fiscal_year_months(fiscal_year)
    month_set = set(months)
    agreements = db.scalars(
        select(RentalAgreement)
        .where(RentalAgreement.is_active.is_(True))
        .order_by(
            RentalAgreement.business,
            RentalAgreement.sort_order,
            RentalAgreement.farm_name,
        )
    ).all()
    agreement_ids = [row.id for row in agreements]

    payments_by_agreement: dict[int, dict[dt.date, float]] = {
        agreement_id: {} for agreement_id in agreement_ids
    }
    if agreement_ids:
        payments = db.scalars(
            select(RentalAgreementPayment).where(
                RentalAgreementPayment.agreement_id.in_(agreement_ids),
                RentalAgreementPayment.payment_month.in_(months),
            )
        ).all()
        for payment in payments:
            if payment.payment_month not in month_set:
                continue
            payments_by_agreement.setdefault(payment.agreement_id, {})[
                payment.payment_month
            ] = float(payment.amount)

    serialized = [
        _serialize_agreement(
            agreement,
            months=months,
            amounts_by_month=payments_by_agreement.get(agreement.id, {}),
        )
        for agreement in agreements
    ]

    by_business: dict[str, list[dict[str, float | None]]] = {
        farm: [] for farm in HERD_FARM_OPTIONS
    }
    for row in serialized:
        by_business.setdefault(row["business"], []).append(row["amounts"])

    business_totals: dict[str, dict[str, Any]] = {}
    for farm in HERD_FARM_OPTIONS:
        amounts = _sum_month_maps(by_business.get(farm, []), months)
        business_totals[farm] = _totals_payload(amounts, months)

    all_amounts = [business_totals[farm]["amounts"] for farm in HERD_FARM_OPTIONS]
    business_totals["Total"] = _totals_payload(
        _sum_month_maps(all_amounts, months), months
    )

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "months": [month.isoformat() for month in months],
        "month_labels": [month.strftime("%b-%y") for month in months],
        "agreements": serialized,
        "business_totals": business_totals,
    }


def build_rental_payment_index(
    db: Session,
    *,
    fiscal_year: int,
) -> dict[tuple[str, dt.date], float]:
    """Monthly rent totals keyed by (business, month) for financial autofill."""
    report = build_rental_agreements_report(db, fiscal_year=fiscal_year)
    index: dict[tuple[str, dt.date], float] = {}
    for farm in HERD_FARM_OPTIONS:
        amounts = report["business_totals"].get(farm, {}).get("amounts", {})
        for month_iso, value in amounts.items():
            if value is None:
                continue
            index[(farm, dt.date.fromisoformat(month_iso))] = float(value)
    return index
