"""Young-stock health index history from SenseHub, joined to DairyComp."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CowEvent, HerdInventory, SenseHubReportSnapshot, SenseHubYoungstockHealth
from app.services.sensehub_api import DEFAULT_REPORT, fetch_named_reports

_UK = ZoneInfo("Europe/London")
_DIGIT_RE = re.compile(r"\d")
SLOTS: tuple[tuple[int, str], ...] = (
    (0, "midnight"),
    (6, "6am"),
    (12, "midday"),
    (18, "6pm"),
)
DEFAULT_THRESHOLD = 86.0


def normalize_animal_id(raw: Any) -> str | None:
    """Keep the first six digits of a SenseHub animal ID; drop letters and the rest."""
    digits = "".join(ch for ch in str(raw or "").replace(" ", "") if ch.isdigit())
    if not digits:
        return None
    return digits[:6]


def etag4(raw: Any) -> str | None:
    """Last four digits of an ID, after stripping spaces."""
    digits = "".join(ch for ch in str(raw or "").replace(" ", "") if ch.isdigit())
    if not digits:
        return None
    return digits[-4:]


def dairycomp_age_days(inventory: HerdInventory | None) -> int | None:
    """Age in days from DairyComp inventory (AGED, else birth date)."""
    if inventory is None:
        return None
    if inventory.aged is not None:
        return int(inventory.aged)
    if inventory.bdat is not None:
        return (dt.date.today() - inventory.bdat).days
    return None


def sample_slot(when: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    """Map a timestamp to the current UK slot: midnight, 6am, midday, 6pm."""
    now = when or dt.datetime.now(_UK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UK)
    else:
        now = now.astimezone(_UK)
    hour = now.hour
    slot_hour, slot_name = SLOTS[0]
    for start, name in SLOTS:
        if hour >= start:
            slot_hour, slot_name = start, name
    sampled = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0, tzinfo=None)
    return sampled, slot_name


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _digit_keys(value: str | None) -> set[str]:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return set()
    keys = {digits}
    keys.add(digits[:6] if len(digits) >= 6 else digits)
    if len(digits) >= 6:
        keys.add(digits[-6:])
    return keys


def save_rows(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    sampled_at: dt.datetime,
    slot: str,
) -> int:
    """Upsert one sample slot of young-stock health rows."""
    saved = 0
    for row in rows:
        animal_id = normalize_animal_id(
            row.get("AnimalID") or row.get("animal_id") or row.get("raw_animal_id")
        )
        if not animal_id:
            continue
        health_index = _to_float(row.get("YoungStockHealthIndex") or row.get("health_index"))
        existing = db.scalar(
            select(SenseHubYoungstockHealth).where(
                SenseHubYoungstockHealth.animal_id == animal_id,
                SenseHubYoungstockHealth.sampled_at == sampled_at,
            )
        )
        raw_id = str(row.get("AnimalID") or row.get("raw_animal_id") or animal_id)
        age_days = _to_int(row.get("AgeInDays") or row.get("age_days"))
        rumination = _to_float(row.get("DailyRumination") or row.get("rumination"))
        eating = _to_float(row.get("DailyEatingTime") or row.get("eating"))
        group_name = row.get("CowGroupName") or row.get("GroupName") or row.get("group_name")
        if existing:
            existing.raw_animal_id = raw_id
            existing.slot = slot
            existing.health_index = health_index
            existing.age_days = age_days
            existing.rumination = rumination
            existing.eating = eating
            existing.group_name = str(group_name) if group_name else None
        else:
            db.add(
                SenseHubYoungstockHealth(
                    animal_id=animal_id,
                    raw_animal_id=raw_id,
                    sampled_at=sampled_at,
                    slot=slot,
                    health_index=health_index,
                    age_days=age_days,
                    rumination=rumination,
                    eating=eating,
                    group_name=str(group_name) if group_name else None,
                )
            )
        saved += 1
    return saved


def save_from_reports(
    db: Session,
    reports: list[dict[str, Any]],
    *,
    sampled_at: dt.datetime | None = None,
) -> int:
    sampled, slot = sample_slot(sampled_at)
    saved = 0
    for report in reports:
        name = str(report.get("report_name") or "")
        if name.casefold() != DEFAULT_REPORT.casefold():
            continue
        saved += save_rows(
            db,
            list(report.get("rows") or []),
            sampled_at=sampled,
            slot=slot,
        )
    return saved


def seed_from_latest_snapshot(db: Session) -> int:
    """If history is empty, copy the latest imported Young Stock Health by Age All report."""
    existing = db.scalar(select(func.count()).select_from(SenseHubYoungstockHealth)) or 0
    if existing:
        return 0
    snapshot = db.scalar(
        select(SenseHubReportSnapshot).where(
            SenseHubReportSnapshot.report_name == DEFAULT_REPORT
        )
    )
    if snapshot is None:
        return 0
    payload = snapshot.payload or {}
    sampled, slot = sample_slot(snapshot.fetched_at)
    saved = save_rows(
        db,
        list(payload.get("rows") or []),
        sampled_at=sampled,
        slot=slot,
    )
    if saved:
        db.commit()
    return saved


def import_youngstock_health(db: Session) -> dict[str, Any]:
    payload = fetch_named_reports([DEFAULT_REPORT])
    reports = payload.get("reports") or []
    sampled, slot = sample_slot()
    saved = save_from_reports(db, reports, sampled_at=sampled)
    db.commit()
    return {
        "saved": saved,
        "sampled_at": sampled.isoformat(),
        "slot": slot,
        "farm_name": payload.get("farm_name"),
    }


def _inventory_indexes(
    db: Session,
) -> tuple[dict[str, HerdInventory], dict[str, HerdInventory]]:
    by_cow: dict[str, HerdInventory] = {}
    by_tag: dict[str, HerdInventory] = {}
    for record in db.scalars(select(HerdInventory)).all():
        for key in _digit_keys(record.cow_id):
            by_cow.setdefault(key, record)
        for key in _digit_keys(record.etag):
            by_tag.setdefault(key, record)
    return by_cow, by_tag


def match_inventory(
    animal_id: str,
    by_cow: dict[str, HerdInventory],
    by_tag: dict[str, HerdInventory],
) -> HerdInventory | None:
    return by_cow.get(animal_id) or by_tag.get(animal_id)


def list_low_health(
    db: Session,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    seed_from_latest_snapshot(db)
    latest = db.scalar(select(func.max(SenseHubYoungstockHealth.sampled_at)))
    if latest is None:
        return {
            "threshold": threshold,
            "sampled_at": None,
            "slot": None,
            "count": 0,
            "animals": [],
        }
    rows = list(
        db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.sampled_at == latest)
            .where(SenseHubYoungstockHealth.health_index.is_not(None))
            .where(SenseHubYoungstockHealth.health_index <= threshold)
            .order_by(
                SenseHubYoungstockHealth.health_index.asc(),
                SenseHubYoungstockHealth.animal_id.asc(),
            )
        ).all()
    )
    by_cow, by_tag = _inventory_indexes(db)
    animals = []
    for row in rows:
        inventory = match_inventory(row.animal_id, by_cow, by_tag)
        etag_value = (inventory.etag if inventory else None) or row.animal_id
        animals.append(
            {
                "animal_id": row.animal_id,
                "etag4": etag4(etag_value) or etag4(row.animal_id),
                "health_index": row.health_index,
                "age_days": dairycomp_age_days(inventory),
                "group_name": row.group_name,
                "farm": inventory.farm if inventory else None,
                "cow_id": inventory.cow_id if inventory else None,
                "etag": (inventory.etag or "").strip() if inventory and inventory.etag else None,
                "pen": inventory.pen if inventory else None,
                "has_dairycomp": inventory is not None,
            }
        )
    animals.sort(
        key=lambda item: (
            int(item["etag4"]) if str(item.get("etag4") or "").isdigit() else 10**9,
            item["animal_id"],
        )
    )
    slot_row = rows[0] if rows else db.scalar(
        select(SenseHubYoungstockHealth).where(
            SenseHubYoungstockHealth.sampled_at == latest
        )
    )
    return {
        "threshold": threshold,
        "sampled_at": latest.isoformat(),
        "slot": slot_row.slot if slot_row else None,
        "count": len(animals),
        "animals": animals,
    }


def animal_events(db: Session, animal_id: str) -> dict[str, Any]:
    animal_id = normalize_animal_id(animal_id) or animal_id
    latest = db.scalar(
        select(SenseHubYoungstockHealth)
        .where(SenseHubYoungstockHealth.animal_id == animal_id)
        .order_by(SenseHubYoungstockHealth.sampled_at.desc())
    )
    by_cow, by_tag = _inventory_indexes(db)
    inventory = match_inventory(animal_id, by_cow, by_tag)
    filters = [CowEvent.cow_id == animal_id]
    if inventory:
        if inventory.cow_id:
            filters.append(CowEvent.cow_id == inventory.cow_id)
        if inventory.etag:
            filters.append(CowEvent.etag == inventory.etag)
    events = list(
        db.scalars(
            select(CowEvent)
            .where(or_(*filters))
            .order_by(CowEvent.event_date.asc(), CowEvent.id.asc())
        ).all()
    )
    birth = inventory.bdat if inventory else None
    if birth is None and latest and latest.age_days is not None:
        birth = dt.date.today() - dt.timedelta(days=latest.age_days)
    if birth is not None:
        events = [
            event
            for event in events
            if event.event_date is None or event.event_date >= birth
        ]
    samples = list(
        db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.animal_id == animal_id)
            .order_by(SenseHubYoungstockHealth.sampled_at.asc())
        ).all()
    )
    slot_labels = {hour_name: label for hour_name, label in (
        ("midnight", "Midnight"),
        ("6am", "6am"),
        ("midday", "Midday"),
        ("6pm", "6pm"),
    )}
    health_history = [
        {
            "sampled_at": sample.sampled_at.isoformat() if sample.sampled_at else None,
            "slot": sample.slot,
            "health_index": sample.health_index,
            "label": (
                f"{sample.sampled_at:%d %b} {slot_labels.get(sample.slot or '', sample.slot or '')}"
                if sample.sampled_at
                else (sample.slot or "")
            ),
        }
        for sample in samples
    ]
    history = [
        {
            "event_date": event.event_date.isoformat() if event.event_date else None,
            "event": event.event,
            "remark": event.remark,
            "protocols": event.protocols,
            "technician": event.technician,
            "dim": event.dim,
            "lact": event.lact,
            "farm": event.farm,
        }
        for event in events
    ]
    return {
        "animal_id": animal_id,
        "health_index": latest.health_index if latest else None,
        "age_days": dairycomp_age_days(inventory),
        "etag4": etag4((inventory.etag if inventory else None) or animal_id),
        "group_name": latest.group_name if latest else None,
        "sampled_at": latest.sampled_at.isoformat() if latest and latest.sampled_at else None,
        "farm": inventory.farm if inventory else None,
        "cow_id": inventory.cow_id if inventory else None,
        "etag": (inventory.etag or "").strip() if inventory and inventory.etag else None,
        "pen": inventory.pen if inventory else None,
        "birth_date": birth.isoformat() if birth else None,
        "has_dairycomp": inventory is not None,
        "health_history": health_history,
        "events": history,
    }
