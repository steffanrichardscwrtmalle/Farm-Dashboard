"""Sales payment tracking for sold animals (Office Admin)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import CowEvent, SalesPaymentRecord, User
from app.services.events_common import (
    SALES_DAIRY_REMARKS,
    SALES_MAPPED_REMARKS,
    SALES_TABLE_REASON_ORDER,
    _sales_reason_expression,
)
from app.services.events_common import normalize_farms

SOLD_EVENT = "SOLD"


def _normalize_key_part(value: str | None) -> str:
    return (value or "").strip()


def _format_gender(gndr: str | None) -> str:
    normalized = (gndr or "").strip().upper()
    if normalized == "M":
        return "Male"
    if normalized == "F":
        return "Female"
    return (gndr or "").strip()


def _age_months_at_sale(bdat: dt.date | None, event_date: dt.date) -> int | None:
    if bdat is None:
        return None
    days = (event_date - bdat).days
    if days < 0:
        return None
    return days // 30


def _payment_match_conditions():
    return and_(
        SalesPaymentRecord.farm == CowEvent.farm,
        SalesPaymentRecord.cow_id == CowEvent.cow_id,
        SalesPaymentRecord.etag == CowEvent.etag,
        SalesPaymentRecord.event_date == CowEvent.event_date,
    )


def _archived_payment_exists():
    return (
        select(literal(1))
        .select_from(SalesPaymentRecord)
        .where(
            _payment_match_conditions(),
            SalesPaymentRecord.archived_at.isnot(None),
        )
        .correlate(CowEvent)
    )


def _reason_filter_conditions(reasons: list[str] | None):
    if not reasons:
        return None
    conditions = []
    if "OFS" in reasons:
        conditions.append(CowEvent.remark == "OFS")
    if "TB" in reasons:
        conditions.append(CowEvent.remark == "CAR11")
    if "Beef" in reasons:
        conditions.append(CowEvent.remark == "CAR16")
    if "Dairy" in reasons:
        conditions.append(CowEvent.remark.in_(list(SALES_DAIRY_REMARKS)))
    if "CULL" in reasons:
        conditions.append(
            or_(
                CowEvent.remark.is_(None),
                CowEvent.remark.notin_(list(SALES_MAPPED_REMARKS)),
            )
        )
    if not conditions:
        return None
    return or_(*conditions)


def _apply_sold_event_filters(
    query,
    *,
    farms: list[str],
    reasons: list[str] | None,
    dest: str | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
):
    query = query.where(CowEvent.event == SOLD_EVENT).where(CowEvent.event_date.isnot(None))
    query = query.where(CowEvent.farm.in_(farms))
    reason_filter = _reason_filter_conditions(reasons)
    if reason_filter is not None:
        query = query.where(reason_filter)
    if dest:
        dest_value = dest.strip()
        if dest_value:
            query = query.where(CowEvent.dest == dest_value)
    if event_from is not None:
        query = query.where(CowEvent.event_date >= event_from)
    if event_to is not None:
        query = query.where(CowEvent.event_date <= event_to)
    return query


def _row_to_dict(
    farm: str,
    cow_id: str | None,
    etag: str | None,
    dest: str | None,
    event_date: dt.date,
    sales_reason: str,
    gndr: str | None = None,
    bdat: dt.date | None = None,
    paid_at: dt.datetime | None = None,
    archived_at: dt.datetime | None = None,
) -> dict[str, Any]:
    normalized_cow_id = _normalize_key_part(cow_id)
    normalized_etag = _normalize_key_part(etag)
    gender = _format_gender(gndr)
    age_months = _age_months_at_sale(bdat, event_date)
    return {
        "farm": farm,
        "cow_id": normalized_cow_id,
        "etag": normalized_etag,
        "gender": gender,
        "age_months": age_months,
        "dest": dest or "",
        "event_date": event_date.isoformat(),
        "sales_reason": sales_reason,
        "paid_at": paid_at.isoformat() if paid_at else None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "payment_key": {
            "farm": farm,
            "cow_id": normalized_cow_id,
            "etag": normalized_etag,
            "event_date": event_date.isoformat(),
        },
    }


def _apply_status_filter(query, status: str):
    if status == "archived":
        return query.where(SalesPaymentRecord.archived_at.isnot(None))
    return query.where(~exists(_archived_payment_exists()))


def _compute_date_bounds(
    db: Session,
    *,
    status: str,
    farms: list[str],
    reasons: list[str] | None,
    dest: str | None,
) -> dict[str, str] | None:
    bounds_query = select(func.min(CowEvent.event_date), func.max(CowEvent.event_date)).select_from(
        CowEvent
    )
    if status == "archived":
        bounds_query = bounds_query.join(
            SalesPaymentRecord,
            _payment_match_conditions(),
        )
    bounds_query = _apply_sold_event_filters(
        bounds_query,
        farms=farms,
        reasons=reasons,
        dest=dest,
        event_from=None,
        event_to=None,
    )
    bounds_query = _apply_status_filter(bounds_query, status)
    min_date, max_date = db.execute(bounds_query).one()
    if min_date is None or max_date is None:
        return None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return {"min": min_date.isoformat(), "max": max_date.isoformat()}


def list_sales_payments(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    reasons: list[str] | None = None,
    dest: str | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    include_date_bounds: bool = True,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "status": status, "date_bounds": None}

    reason_expr = _sales_reason_expression()
    dest_nulls_last = case((CowEvent.dest.is_(None), 1), else_=0)
    etag_suffix = func.substr(func.coalesce(CowEvent.etag, ""), -5)

    if status == "archived":
        query = (
            select(
                CowEvent.farm,
                CowEvent.cow_id,
                CowEvent.etag,
                CowEvent.dest,
                CowEvent.event_date,
                reason_expr.label("sales_reason"),
                CowEvent.gndr,
                CowEvent.bdat,
                SalesPaymentRecord.paid_at,
                SalesPaymentRecord.archived_at,
            )
            .select_from(CowEvent)
            .join(SalesPaymentRecord, _payment_match_conditions())
        )
    else:
        query = select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.dest,
            CowEvent.event_date,
            reason_expr.label("sales_reason"),
            CowEvent.gndr,
            CowEvent.bdat,
            literal(None).label("paid_at"),
            literal(None).label("archived_at"),
        )

    query = _apply_sold_event_filters(
        query,
        farms=selected_farms,
        reasons=reasons,
        dest=dest,
        event_from=event_from,
        event_to=event_to,
    )
    query = _apply_status_filter(query, status)
    query = query.order_by(
        CowEvent.event_date.asc(),
        dest_nulls_last.asc(),
        CowEvent.dest.asc(),
        etag_suffix.asc(),
    )

    date_bounds = None
    if include_date_bounds:
        date_bounds = _compute_date_bounds(
            db,
            status=status,
            farms=selected_farms,
            reasons=reasons,
            dest=dest,
        )

    rows = []
    for (
        farm,
        cow_id,
        etag,
        dest,
        event_date,
        sales_reason,
        gndr,
        bdat,
        paid_at,
        archived_at,
    ) in db.execute(query).all():
        rows.append(
            _row_to_dict(
                farm,
                cow_id,
                etag,
                dest,
                event_date,
                sales_reason,
                gndr,
                bdat,
                paid_at,
                archived_at,
            )
        )

    return {"rows": rows, "total": len(rows), "status": status, "date_bounds": date_bounds}


def list_dest_filter_options(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"dest_options": [], "date_bounds": None}

    dest_query = select(func.distinct(CowEvent.dest)).where(CowEvent.dest.isnot(None)).where(
        func.trim(CowEvent.dest) != ""
    )
    if status == "archived":
        dest_query = dest_query.select_from(CowEvent).join(
            SalesPaymentRecord,
            _payment_match_conditions(),
        )
    dest_query = _apply_sold_event_filters(
        dest_query,
        farms=selected_farms,
        reasons=reasons,
        dest=None,
        event_from=None,
        event_to=None,
    )
    dest_query = _apply_status_filter(dest_query, status)
    dest_query = dest_query.order_by(CowEvent.dest.asc())
    dest_rows = db.execute(dest_query).all()
    date_bounds = _compute_date_bounds(
        db,
        status=status,
        farms=selected_farms,
        reasons=reasons,
        dest=None,
    )
    return {
        "dest_options": [row[0] for row in dest_rows if row[0]],
        "date_bounds": date_bounds,
    }


def normalize_sales_reasons(reasons: list[str] | None) -> list[str]:
    if not reasons:
        return list(SALES_TABLE_REASON_ORDER)
    selected: list[str] = []
    for value in reasons:
        normalized = value.strip().upper()
        if normalized == "BEEF":
            normalized = "Beef"
        for reason in SALES_TABLE_REASON_ORDER:
            if reason.upper() == normalized and reason not in selected:
                selected.append(reason)
    return selected or list(SALES_TABLE_REASON_ORDER)


def _payment_record_key(
    farm: str,
    cow_id: str,
    etag: str,
    event_date: dt.date,
) -> tuple[str, str, str, dt.date]:
    return (farm, _normalize_key_part(cow_id), _normalize_key_part(etag), event_date)


def _parse_payment_item(item: dict[str, Any]) -> tuple[str, str, str, dt.date]:
    farm = item["farm"]
    cow_id = _normalize_key_part(item.get("cow_id"))
    etag = _normalize_key_part(item.get("etag"))
    event_date = item["event_date"]
    if isinstance(event_date, str):
        event_date = dt.date.fromisoformat(event_date)
    return farm, cow_id, etag, event_date


def _load_payment_records_for_items(
    db: Session,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str, str, dt.date], SalesPaymentRecord]:
    parsed = [_parse_payment_item(item) for item in items]
    if not parsed:
        return {}

    conditions = [
        and_(
            SalesPaymentRecord.farm == farm,
            func.coalesce(SalesPaymentRecord.cow_id, "") == cow_id,
            func.coalesce(SalesPaymentRecord.etag, "") == etag,
            SalesPaymentRecord.event_date == event_date,
        )
        for farm, cow_id, etag, event_date in parsed
    ]
    records = db.scalars(select(SalesPaymentRecord).where(or_(*conditions))).all()
    return {
        _payment_record_key(
            record.farm,
            record.cow_id,
            record.etag,
            record.event_date,
        ): record
        for record in records
    }


def _find_payment_record(
    db: Session,
    *,
    farm: str,
    cow_id: str,
    etag: str,
    event_date: dt.date,
) -> SalesPaymentRecord | None:
    return db.scalar(
        select(SalesPaymentRecord).where(
            SalesPaymentRecord.farm == farm,
            func.coalesce(SalesPaymentRecord.cow_id, "") == cow_id,
            func.coalesce(SalesPaymentRecord.etag, "") == etag,
            SalesPaymentRecord.event_date == event_date,
        )
    )


def confirm_payments(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_payment_records_for_items(db, items)
    confirmed = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_payment_item(item)
        key = _payment_record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record:
            record.paid_at = now
            record.archived_at = now
            record.confirmed_by_user_id = user.id
            record.unarchived_at = None
        else:
            db.add(
                SalesPaymentRecord(
                    farm=farm,
                    cow_id=cow_id,
                    etag=etag,
                    event_date=event_date,
                    paid_at=now,
                    archived_at=now,
                    confirmed_by_user_id=user.id,
                )
            )
        confirmed += 1

    db.commit()
    return {"confirmed": confirmed}


def unarchive_payments(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_payment_records_for_items(db, items)
    restored = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_payment_item(item)
        key = _payment_record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record is None or record.archived_at is None:
            continue
        record.archived_at = None
        record.paid_at = None
        record.unarchived_at = now
        record.confirmed_by_user_id = user.id
        restored += 1

    db.commit()
    return {"restored": restored}
