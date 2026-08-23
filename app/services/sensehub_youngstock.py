"""Young-stock health index history from SenseHub, joined to DairyComp."""

from __future__ import annotations

import datetime as dt
import re
import threading
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CowEvent, HerdInventory, SenseHubReportSnapshot, SenseHubYoungstockHealth
from app.services.events_common import filter_disease_episode_records
from app.services.sensehub_api import (
    DEFAULT_REPORT,
    SenseHubError,
    fetch_named_reports,
    fetch_report,
    flatten_report,
    list_reports,
    login,
)

_UK = ZoneInfo("Europe/London")
_DIGIT_RE = re.compile(r"\d")
SLOTS: tuple[tuple[int, str], ...] = (
    (0, "midnight"),
    (6, "6am"),
    (12, "midday"),
    (18, "6pm"),
)
DEFAULT_THRESHOLD = 86.0
MAX_BACKFILL_DAYS = 730
EMPTY_SLOT_STOP = 28
TREND_DOTS = 12
CHART_EVENT_LETTERS: dict[str, str] = {
    "RESP": "R",
    "SCOURS": "S",
    "ILL": "I",
    "VACC": "V",
}
TREATMENT_EVENTS = frozenset({"RESP", "SCOURS", "ILL"})
LAST_TREATMENT_EVENTS = frozenset({"RESP", "ILL"})


def health_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 80:
        return "red"
    if value < 85:
        return "yellow"
    if value < 90:
        return "blue"
    return "green"


def trend_dots(indexes: list[float | None]) -> list[dict[str, Any]]:
    recent = list(indexes[-TREND_DOTS:])
    padded: list[float | None] = [None] * (TREND_DOTS - len(recent)) + recent
    return [{"health_index": value, "band": health_band(value)} for value in padded]


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


def _event_code(name: Any) -> str:
    return str(name or "").strip().upper()


def _event_date_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text[:10] if text else None


def treatment_episodes(events: list[CowEvent]) -> list[dict[str, Any]]:
    """RESP, SCOURS, and ILL events after the shared disease episode gap."""
    records = [
        {
            "cow_id": event.cow_id,
            "etag": event.etag,
            "event": _event_code(event.event),
            "event_date": event.event_date,
            "farm": event.farm,
        }
        for event in events
        if _event_code(event.event) in TREATMENT_EVENTS
    ]
    return filter_disease_episode_records(records)


def days_since_last_treatment(
    events: list[CowEvent],
    *,
    today: dt.date | None = None,
) -> int | None:
    """Days since the most recent ILL or RESP event."""
    today = today or dt.date.today()
    latest: dt.date | None = None
    for event in events:
        if _event_code(event.event) not in LAST_TREATMENT_EVENTS:
            continue
        if event.event_date is None:
            continue
        if latest is None or event.event_date > latest:
            latest = event.event_date
    if latest is None:
        return None
    return (today - latest).days


def _events_for_animals(
    db: Session,
    animals: list[tuple[str, HerdInventory | None]],
) -> dict[str, list[CowEvent]]:
    cow_ids: set[str] = set()
    etags: set[str] = set()
    for animal_id, inventory in animals:
        cow_ids.add(animal_id)
        if inventory is None:
            continue
        if inventory.cow_id:
            cow_ids.add(inventory.cow_id)
        if inventory.etag:
            etags.add(inventory.etag)
    if not cow_ids and not etags:
        return {animal_id: [] for animal_id, _inventory in animals}
    filters = []
    if cow_ids:
        filters.append(CowEvent.cow_id.in_(cow_ids))
    if etags:
        filters.append(CowEvent.etag.in_(etags))
    events = list(db.scalars(select(CowEvent).where(or_(*filters))).all())
    by_cow: dict[str, list[CowEvent]] = defaultdict(list)
    by_etag: dict[str, list[CowEvent]] = defaultdict(list)
    for event in events:
        if event.cow_id:
            by_cow[event.cow_id].append(event)
        if event.etag:
            by_etag[event.etag].append(event)
    result: dict[str, list[CowEvent]] = {}
    for animal_id, inventory in animals:
        seen: set[int] = set()
        collected: list[CowEvent] = []
        keys = {animal_id}
        if inventory is not None and inventory.cow_id:
            keys.add(inventory.cow_id)
        for key in keys:
            for event in by_cow.get(key, []):
                if event.id not in seen:
                    seen.add(event.id)
                    collected.append(event)
        if inventory is not None and inventory.etag:
            for event in by_etag.get(inventory.etag, []):
                if event.id not in seen:
                    seen.add(event.id)
                    collected.append(event)
        birth = inventory.bdat if inventory is not None else None
        if birth is not None:
            collected = [
                event
                for event in collected
                if event.event_date is None or event.event_date >= birth
            ]
        result[animal_id] = collected
    return result


def treatment_counts(events: list[CowEvent]) -> dict[str, int]:
    """Count pneumonia, scours, and illness episodes, not repeat treatments."""
    counts = {"resp_count": 0, "scours_count": 0, "ill_count": 0}
    for record in treatment_episodes(events):
        code = record["event"]
        if code == "RESP":
            counts["resp_count"] += 1
        elif code == "SCOURS":
            counts["scours_count"] += 1
        elif code == "ILL":
            counts["ill_count"] += 1
    return counts


def chart_event_markers(events: list[CowEvent]) -> list[dict[str, str]]:
    """One R/S/I/V icon for every event date. Counts still use the episode gap."""
    seen: set[tuple[str, str]] = set()
    markers: list[dict[str, str]] = []
    for event in events:
        code = _event_code(event.event)
        letter = CHART_EVENT_LETTERS.get(code)
        date_str = _event_date_iso(event.event_date)
        if not letter or not date_str:
            continue
        key = (date_str, letter)
        if key in seen:
            continue
        seen.add(key)
        markers.append({"date": date_str, "letter": letter, "event": code})
    markers.sort(key=lambda item: (item["date"], item["letter"]))
    return markers


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


def past_slots(
    days: int,
    *,
    now: dt.datetime | None = None,
) -> list[tuple[dt.datetime, str, int]]:
    """UK midnight/6am/midday/6pm slots in the last `days` days, excluding the future."""
    current = now or dt.datetime.now(_UK)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UK)
    else:
        current = current.astimezone(_UK)
    slots: list[tuple[dt.datetime, str, int]] = []
    start_day = current.date() - dt.timedelta(days=max(0, days))
    day = start_day
    while day <= current.date():
        for hour, name in SLOTS:
            sampled_uk = dt.datetime(day.year, day.month, day.day, hour, tzinfo=_UK)
            if sampled_uk >= current:
                continue
            naive = sampled_uk.replace(tzinfo=None)
            slots.append((naive, name, int(sampled_uk.timestamp())))
        day += dt.timedelta(days=1)
    return slots


def slots_to_fetch(
    all_slots: list[tuple[dt.datetime, str, int]],
    existing: set[dt.datetime],
    *,
    catch_up: bool,
    current: dt.datetime | None = None,
) -> list[tuple[dt.datetime, str, int]]:
    """Newest-first slots to pull from SenseHub.

    Catch-up keeps the current slot (even if already stored) then walks back
    through missing times until it meets already-saved history. A full fill
    requests every missing slot in the span.
    """
    newest_first = list(reversed(all_slots))
    if not catch_up:
        return [item for item in newest_first if item[0] not in existing]
    pending: list[tuple[dt.datetime, str, int]] = []
    for item in newest_first:
        sampled = item[0]
        if sampled in existing and (current is None or sampled != current):
            break
        pending.append(item)
    return pending


def backfill_span_days(db: Session, *, now: dt.datetime | None = None) -> int:
    """Days back to the oldest current youngstock animal's birth / SenseHub age."""
    current = now or dt.datetime.now(_UK)
    if current.tzinfo is None:
        today = current.date()
    else:
        today = current.astimezone(_UK).date()
    max_age = int(db.scalar(select(func.max(SenseHubYoungstockHealth.age_days))) or 0)
    latest = db.scalar(select(func.max(SenseHubYoungstockHealth.sampled_at)))
    animal_ids: list[str] = []
    if latest is not None:
        animal_ids = list(
            db.scalars(
                select(SenseHubYoungstockHealth.animal_id).where(
                    SenseHubYoungstockHealth.sampled_at == latest
                )
            ).all()
        )
    by_cow, by_tag = _inventory_indexes(db)
    earliest_birth: dt.date | None = None
    for animal_id in animal_ids:
        inventory = match_inventory(animal_id, by_cow, by_tag)
        age = dairycomp_age_days(inventory)
        if age:
            max_age = max(max_age, int(age))
        if inventory is not None and inventory.bdat is not None:
            if earliest_birth is None or inventory.bdat < earliest_birth:
                earliest_birth = inventory.bdat
    if earliest_birth is not None:
        max_age = max(max_age, (today - earliest_birth).days)
    if max_age <= 0:
        max_age = 365
    return max(1, min(max_age, MAX_BACKFILL_DAYS))


_job_lock = threading.Lock()
_job_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "slots_done": 0,
    "slots_total": 0,
}


def get_youngstock_job_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_status)


def _set_job(**kwargs: Any) -> None:
    with _job_lock:
        _job_status.update(kwargs)


def is_youngstock_job_running() -> bool:
    with _job_lock:
        return _job_status.get("status") == "running"


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


def _import_current_slot(db: Session) -> dict[str, Any]:
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


def import_youngstock_health(db: Session) -> dict[str, Any]:
    """Fetch the current slot, then any older missing slots until stored history.

    Cron can therefore catch up after a failed run, and only pulls the latest
    sample when every older slot is already saved.
    """
    sampled, slot = sample_slot()
    result = backfill_youngstock_health(db, days=None, catch_up=True)
    result["sampled_at"] = sampled.isoformat()
    result["slot"] = slot
    return result


def backfill_youngstock_health(
    db: Session,
    *,
    days: int | None = None,
    catch_up: bool = False,
) -> dict[str, Any]:
    """Re-run Young Stock Health by Age All at past UK sample slots.

    When `days` is None, walk back to the oldest current calf's birth (or SenseHub
    age). Slots already stored are skipped. Catch-up mode fetches the current
    slot, then only the missing older ones until saved history is reached.
    """
    if db.scalar(select(func.count()).select_from(SenseHubYoungstockHealth)) == 0:
        try:
            _import_current_slot(db)
        except Exception:
            db.rollback()
    span = days if days is not None else backfill_span_days(db)
    all_slots = past_slots(span)
    existing = set(db.scalars(select(SenseHubYoungstockHealth.sampled_at).distinct()).all())
    current, _slot_name = sample_slot()
    slots = slots_to_fetch(
        all_slots,
        existing,
        catch_up=catch_up,
        current=current if catch_up else None,
    )
    stop_on_empty = days is None
    _set_job(
        status="running",
        message=(
            f"Logging in to SenseHub to fill {len(slots)} missing slots "
            f"(up to {span} days)…"
        ),
        slots_done=0,
        slots_total=len(slots),
    )
    saved_total = 0
    errors: list[str] = []
    empty_streak = 0
    fetched = 0
    try:
        with httpx.Client(timeout=90.0, follow_redirects=True) as client:
            token, version = login(client)
            catalog = list_reports(client, token)
            item = next(
                (
                    entry
                    for entry in catalog
                    if str(entry.get("name") or "").casefold() == DEFAULT_REPORT.casefold()
                ),
                None,
            )
            if item is None or item.get("key") is None:
                raise SenseHubError(f"SenseHub catalogue has no {DEFAULT_REPORT}.")
            key = int(item["key"])
            for index, (sampled, slot, unix) in enumerate(slots, 1):
                _set_job(
                    message=f"Backfilling {slot} {sampled:%d %b %Y} ({index}/{len(slots)})…",
                    slots_done=index - 1,
                    slots_total=len(slots),
                )
                try:
                    raw = fetch_report(
                        client,
                        token,
                        key,
                        cloud=True,
                        display_version=version,
                        past_report_time=unix,
                    )
                    rows = list(flatten_report(raw, catalog_item=item).get("rows") or [])
                    saved_total += save_rows(
                        db,
                        rows,
                        sampled_at=sampled,
                        slot=slot,
                    )
                    db.commit()
                    fetched += 1
                    if rows:
                        empty_streak = 0
                    else:
                        empty_streak += 1
                except Exception as exc:
                    db.rollback()
                    errors.append(f"{sampled:%Y-%m-%d} {slot}: {exc}")
                    empty_streak += 1
                if stop_on_empty and empty_streak >= EMPTY_SLOT_STOP:
                    _set_job(
                        message=(
                            f"Reached the start of SenseHub history after "
                            f"{sampled:%d %b %Y}."
                        )
                    )
                    break
        _set_job(
            status="complete",
            message=(
                f"Backfilled {fetched} time slots ({saved_total} rows)"
                + (f", {len(errors)} failed." if errors else ".")
            ),
            slots_done=len(slots),
            slots_total=len(slots),
        )
        return {
            "saved": saved_total,
            "slots": len(slots),
            "span_days": span,
            "errors": errors,
        }
    except Exception as exc:
        _set_job(status="error", message=str(exc))
        raise


def run_backfill_in_background(db_factory, days: int | None = None) -> None:
    db = db_factory()
    try:
        backfill_youngstock_health(db, days=days)
    except Exception:
        pass
    finally:
        db.close()


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
    animal_ids = [row.animal_id for row in rows]
    history_by_animal: dict[str, list[float | None]] = defaultdict(list)
    if animal_ids:
        history_rows = db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.animal_id.in_(animal_ids))
            .order_by(
                SenseHubYoungstockHealth.animal_id.asc(),
                SenseHubYoungstockHealth.sampled_at.asc(),
            )
        ).all()
        for sample in history_rows:
            history_by_animal[sample.animal_id].append(sample.health_index)
    by_cow, by_tag = _inventory_indexes(db)
    matched = [
        (row.animal_id, match_inventory(row.animal_id, by_cow, by_tag))
        for row in rows
    ]
    events_by_animal = _events_for_animals(db, matched)
    animals = []
    for row, (_animal_id, inventory) in zip(rows, matched, strict=True):
        etag_value = (inventory.etag if inventory else None) or row.animal_id
        events = events_by_animal.get(row.animal_id, [])
        animals.append(
            {
                "animal_id": row.animal_id,
                "etag4": etag4(etag_value) or etag4(row.animal_id),
                "health_index": row.health_index,
                "age_days": dairycomp_age_days(inventory),
                "days_since_last_treatment": days_since_last_treatment(events),
                "resp_count": treatment_counts(events)["resp_count"],
                "trend": trend_dots(history_by_animal.get(row.animal_id, [])),
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
            .order_by(CowEvent.event_date.desc(), CowEvent.id.desc())
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
    markers = chart_event_markers(events)
    sampled_dates = {
        sample.sampled_at.date().isoformat()
        for sample in samples
        if sample.sampled_at
    }
    for marker in markers:
        if marker["date"] in sampled_dates:
            continue
        sampled_dates.add(marker["date"])
        day = dt.date.fromisoformat(marker["date"])
        health_history.append(
            {
                "sampled_at": f"{marker['date']}T12:00:00",
                "slot": None,
                "health_index": None,
                "label": f"{day:%d %b}",
            }
        )
    health_history.sort(key=lambda item: item.get("sampled_at") or "")
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
        **treatment_counts(events),
        "chart_markers": markers,
    }
