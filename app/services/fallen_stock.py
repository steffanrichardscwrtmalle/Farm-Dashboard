"""Fallen stock collection tracking for dead animals (Office Admin)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import CowEvent, FallenStockRecord, User
from app.services.events_common import normalize_farms

DIED_EVENT = "DIED"


def _normalize_key_part(value: str | None) -> str:
    return (value or "").strip()


def _format_gender(gndr: str | None) -> str:
    normalized = (gndr or "").strip().upper()
    if normalized == "M":
        return "Male"
    if normalized == "F":
        return "Female"
    return (gndr or "").strip()


def _age_months_at_death(bdat: dt.date | None, event_date: dt.date) -> int | None:
    if bdat is None:
        return None
    days = (event_date - bdat).days
    if days < 0:
        return None
    return days // 30


def _format_remark(remark: str | None) -> str:
    value = (remark or "").strip()
    return value if value else "—"


def _record_match_conditions():
    return and_(
        FallenStockRecord.farm == CowEvent.farm,
        FallenStockRecord.cow_id == CowEvent.cow_id,
        FallenStockRecord.etag == CowEvent.etag,
        FallenStockRecord.event_date == CowEvent.event_date,
    )


def _archived_record_exists():
    return (
        select(literal(1))
        .select_from(FallenStockRecord)
        .where(
            _record_match_conditions(),
            FallenStockRecord.archived_at.isnot(None),
        )
        .correlate(CowEvent)
    )


def _apply_died_event_filters(
    query,
    *,
    farms: list[str],
    dest: str | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
):
    query = query.where(CowEvent.event == DIED_EVENT).where(CowEvent.event_date.isnot(None))
    query = query.where(CowEvent.farm.in_(farms))
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
    remark: str | None,
    gndr: str | None = None,
    bdat: dt.date | None = None,
    collected_at: dt.datetime | None = None,
    archived_at: dt.datetime | None = None,
) -> dict[str, Any]:
    normalized_cow_id = _normalize_key_part(cow_id)
    normalized_etag = _normalize_key_part(etag)
    gender = _format_gender(gndr)
    age_months = _age_months_at_death(bdat, event_date)
    return {
        "farm": farm,
        "cow_id": normalized_cow_id,
        "etag": normalized_etag,
        "gender": gender,
        "age_months": age_months,
        "dest": dest or "",
        "event_date": event_date.isoformat(),
        "remark": _format_remark(remark),
        "collected_at": collected_at.isoformat() if collected_at else None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "record_key": {
            "farm": farm,
            "cow_id": normalized_cow_id,
            "etag": normalized_etag,
            "event_date": event_date.isoformat(),
        },
    }


def _apply_status_filter(query, status: str):
    if status == "archived":
        return query.where(FallenStockRecord.archived_at.isnot(None))
    return query.where(~exists(_archived_record_exists()))


def _compute_date_bounds(
    db: Session,
    *,
    status: str,
    farms: list[str],
    dest: str | None,
) -> dict[str, str] | None:
    bounds_query = select(func.min(CowEvent.event_date), func.max(CowEvent.event_date)).select_from(
        CowEvent
    )
    if status == "archived":
        bounds_query = bounds_query.join(
            FallenStockRecord,
            _record_match_conditions(),
        )
    bounds_query = _apply_died_event_filters(
        bounds_query,
        farms=farms,
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


def list_fallen_stock(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    dest: str | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    include_date_bounds: bool = True,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "status": status, "date_bounds": None}

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
                CowEvent.remark,
                CowEvent.gndr,
                CowEvent.bdat,
                FallenStockRecord.collected_at,
                FallenStockRecord.archived_at,
            )
            .select_from(CowEvent)
            .join(FallenStockRecord, _record_match_conditions())
        )
    else:
        query = select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.dest,
            CowEvent.event_date,
            CowEvent.remark,
            CowEvent.gndr,
            CowEvent.bdat,
            literal(None).label("collected_at"),
            literal(None).label("archived_at"),
        )

    query = _apply_died_event_filters(
        query,
        farms=selected_farms,
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
            dest=dest,
        )

    rows = []
    for (
        farm,
        cow_id,
        etag,
        dest_value,
        event_date,
        remark,
        gndr,
        bdat,
        collected_at,
        archived_at,
    ) in db.execute(query).all():
        rows.append(
            _row_to_dict(
                farm,
                cow_id,
                etag,
                dest_value,
                event_date,
                remark,
                gndr,
                bdat,
                collected_at,
                archived_at,
            )
        )

    return {"rows": rows, "total": len(rows), "status": status, "date_bounds": date_bounds}


def list_dest_filter_options(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"dest_options": [], "date_bounds": None}

    dest_query = select(func.distinct(CowEvent.dest)).where(CowEvent.dest.isnot(None)).where(
        func.trim(CowEvent.dest) != ""
    )
    if status == "archived":
        dest_query = dest_query.select_from(CowEvent).join(
            FallenStockRecord,
            _record_match_conditions(),
        )
    dest_query = _apply_died_event_filters(
        dest_query,
        farms=selected_farms,
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
        dest=None,
    )
    return {
        "dest_options": [row[0] for row in dest_rows if row[0]],
        "date_bounds": date_bounds,
    }


def _record_key(
    farm: str,
    cow_id: str,
    etag: str,
    event_date: dt.date,
) -> tuple[str, str, str, dt.date]:
    return (farm, _normalize_key_part(cow_id), _normalize_key_part(etag), event_date)


def _parse_record_item(item: dict[str, Any]) -> tuple[str, str, str, dt.date]:
    farm = item["farm"]
    cow_id = _normalize_key_part(item.get("cow_id"))
    etag = _normalize_key_part(item.get("etag"))
    event_date = item["event_date"]
    if isinstance(event_date, str):
        event_date = dt.date.fromisoformat(event_date)
    return farm, cow_id, etag, event_date


def _load_records_for_items(
    db: Session,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str, str, dt.date], FallenStockRecord]:
    parsed = [_parse_record_item(item) for item in items]
    if not parsed:
        return {}

    conditions = [
        and_(
            FallenStockRecord.farm == farm,
            func.coalesce(FallenStockRecord.cow_id, "") == cow_id,
            func.coalesce(FallenStockRecord.etag, "") == etag,
            FallenStockRecord.event_date == event_date,
        )
        for farm, cow_id, etag, event_date in parsed
    ]
    records = db.scalars(select(FallenStockRecord).where(or_(*conditions))).all()
    return {
        _record_key(
            record.farm,
            record.cow_id,
            record.etag,
            record.event_date,
        ): record
        for record in records
    }


def confirm_collections(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_records_for_items(db, items)
    confirmed = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_record_item(item)
        key = _record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record:
            record.collected_at = now
            record.archived_at = now
            record.confirmed_by_user_id = user.id
            record.unarchived_at = None
        else:
            db.add(
                FallenStockRecord(
                    farm=farm,
                    cow_id=cow_id,
                    etag=etag,
                    event_date=event_date,
                    collected_at=now,
                    archived_at=now,
                    confirmed_by_user_id=user.id,
                )
            )
        confirmed += 1

    db.commit()
    return {"confirmed": confirmed}


def unarchive_collections(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_records_for_items(db, items)
    restored = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_record_item(item)
        key = _record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record is None or record.archived_at is None:
            continue
        record.archived_at = None
        record.collected_at = None
        record.unarchived_at = now
        record.confirmed_by_user_id = user.id
        restored += 1

    db.commit()
    return {"restored": restored}
