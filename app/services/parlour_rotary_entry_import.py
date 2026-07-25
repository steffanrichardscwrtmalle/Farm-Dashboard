"""Import Rotary Entry ID reports and match lag phase onto milk-flow rows."""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    ParlourMilkFlowRow,
    ParlourRotaryEntryIdEvent,
    ParlourRotaryEntryIdImport,
)
from app.services.parlour_rotary_entry_parse import (
    parse_rotary_entry_id_report,
)

logger = logging.getLogger(__name__)

# Prep / ID must fall this many seconds before cluster attach.
LAG_WINDOW_SECONDS = 180


def _parse_received(
    value: dt.datetime | str | None,
) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def match_rotary_entry_ids_to_milkings(
    db: Session,
    *,
    farm: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict[str, Any]:
    """Match ID events to milkings in [date_from, date_to] and store lag phase.

    Clears lag fields only for milkings in that window, then rematches.
    One ID event is used by at most one milking (greedy by milking time).
    """
    farm_key = farm.upper()
    milkings = list(
        db.scalars(
            select(ParlourMilkFlowRow)
            .where(
                ParlourMilkFlowRow.farm == farm_key,
                ParlourMilkFlowRow.milking_date >= date_from,
                ParlourMilkFlowRow.milking_date <= date_to,
            )
            .order_by(
                ParlourMilkFlowRow.milking_date,
                ParlourMilkFlowRow.start_seconds,
                ParlourMilkFlowRow.id,
            )
        ).all()
    )

    for row in milkings:
        row.identification_seconds = None
        row.lag_phase_seconds = None

    id_from = dt.datetime.combine(date_from, dt.time.min) - dt.timedelta(days=1)
    id_to = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min)

    events = list(
        db.scalars(
            select(ParlourRotaryEntryIdEvent)
            .where(
                ParlourRotaryEntryIdEvent.farm == farm_key,
                ParlourRotaryEntryIdEvent.identified_at >= id_from,
                ParlourRotaryEntryIdEvent.identified_at < id_to,
            )
            .order_by(ParlourRotaryEntryIdEvent.identified_at)
        ).all()
    )

    by_cow: dict[str, list[ParlourRotaryEntryIdEvent]] = defaultdict(list)
    for event in events:
        by_cow[event.cow_id].append(event)

    used_event_ids: set[int] = set()
    matched = 0
    eligible = 0

    for row in milkings:
        if not row.cow_id or row.start_seconds is None:
            continue
        eligible += 1
        milking_dt = dt.datetime.combine(row.milking_date, dt.time.min) + dt.timedelta(
            seconds=int(row.start_seconds)
        )
        best: ParlourRotaryEntryIdEvent | None = None
        best_lag: float | None = None
        for event in by_cow.get(row.cow_id, []):
            if event.id in used_event_ids:
                continue
            lag = (milking_dt - event.identified_at).total_seconds()
            if 0 <= lag <= LAG_WINDOW_SECONDS and (
                best_lag is None or lag < best_lag
            ):
                best = event
                best_lag = lag
        if best is None or best_lag is None:
            continue
        used_event_ids.add(best.id)
        row.identification_seconds = best.id_seconds
        row.lag_phase_seconds = int(round(best_lag))
        matched += 1

    db.flush()
    return {
        "farm": farm_key,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "milkings": eligible,
        "matched": matched,
    }


def import_rotary_entry_id_bytes(
    db: Session,
    content: bytes,
    *,
    filename: str = "",
    farm: str | None = None,
    source_message_id: str | None = None,
    source_received: dt.datetime | str | None = None,
) -> dict[str, Any]:
    """Upsert Rotary Entry ID events, then rematch milkings in the affected range."""
    parsed = parse_rotary_entry_id_report(content, filename=filename, farm=farm)
    received = _parse_received(source_received)

    batch = ParlourRotaryEntryIdImport(
        farm=parsed.farm,
        source_filename=parsed.source_filename or filename,
        source_message_id=source_message_id,
        source_received=received,
        rows_imported=0,
    )
    db.add(batch)
    db.flush()

    id_min = min(e.identified_at for e in parsed.events)
    id_max = max(e.identified_at for e in parsed.events)
    # Cumulative exports replace the covered calendar window so a corrected
    # Date+time parse is not blocked by earlier wrong-timestamp rows.
    range_start = dt.datetime.combine(id_min.date(), dt.time.min)
    range_end = dt.datetime.combine(id_max.date() + dt.timedelta(days=1), dt.time.min)
    db.execute(
        delete(ParlourRotaryEntryIdEvent).where(
            ParlourRotaryEntryIdEvent.farm == parsed.farm,
            ParlourRotaryEntryIdEvent.identified_at >= range_start,
            ParlourRotaryEntryIdEvent.identified_at < range_end,
        )
    )

    new_events = [
        ParlourRotaryEntryIdEvent(
            import_id=batch.id,
            farm=parsed.farm,
            cow_id=event.cow_id,
            identified_at=event.identified_at,
            id_seconds=event.id_seconds,
        )
        for event in parsed.events
    ]
    db.add_all(new_events)
    batch.rows_imported = len(new_events)
    db.flush()

    # Rematch milkings that could use these IDs (include ±1 day for midnight wrap).
    date_from = id_min.date() - dt.timedelta(days=1)
    date_to = id_max.date() + dt.timedelta(days=1)
    match_stats = match_rotary_entry_ids_to_milkings(
        db,
        farm=parsed.farm,
        date_from=date_from,
        date_to=date_to,
    )
    db.commit()

    logger.info(
        "Imported Rotary Entry ID farm=%s events=%s matched=%s/%s range=%s..%s",
        parsed.farm,
        len(new_events),
        match_stats["matched"],
        match_stats["milkings"],
        id_min.date(),
        id_max.date(),
    )
    return {
        "ok": True,
        "skipped": False,
        "kind": "rotary_entry_id",
        "import_id": batch.id,
        "farm": parsed.farm,
        "rows_imported": batch.rows_imported,
        "rows_parsed": len(parsed.events),
        "source_filename": batch.source_filename,
        "match": match_stats,
    }


def rematch_lag_for_milk_flow(
    db: Session,
    *,
    farm: str,
    milking_date: dt.date,
) -> dict[str, Any]:
    """Re-run lag matching after a milk-flow shift arrives for a date."""
    stats = match_rotary_entry_ids_to_milkings(
        db,
        farm=farm,
        date_from=milking_date,
        date_to=milking_date,
    )
    db.commit()
    return stats
