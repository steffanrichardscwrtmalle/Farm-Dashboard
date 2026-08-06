"""Build the pending BCMS movement / birth / death queue for Record Movements."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import cts_farm_credentials
from app.models import (
    CowEvent,
    CtsOnHolding,
    CtsReportedMovement,
    HerdBirth,
    HerdInventory,
    PedigreeRegistrationRecord,
    StockPurchaseAnimal,
)
from app.services.cts_client import normalize_cts_etag
from app.services.bcms_breeds import bcms_breed_from_cbrd

logger = logging.getLogger(__name__)

MOVEMENT_TYPES = ("birth", "sale", "death", "move_on")
_UK = ZoneInfo("Europe/London")
_ACTIVE_REPORT_STATUSES = ("sent", "ok", "accepted")
_SUPPRESS_PENDING_STATUSES = ("sent", "ok", "accepted", "archived")
_STALE_AWAITING_MESSAGE = (
    "Not reflected on CTS cattle-on-holding after overnight refresh; "
    "returned to pending to retry."
)
_ARCHIVED_MESSAGE = "Confirmed on CTS cattle-on-holding."


def _inventory_is_move_on(inv: HerdInventory) -> bool:
    """Bought-in / enrolled: inventory EDAT present and different from BDAT.

    Born-on-farm animals have EDAT = BDAT. Prefer inventory EDAT over events.
    """
    if inv.edat is None or inv.bdat is None:
        return False
    return inv.edat != inv.bdat


def _holding_for(farm: str) -> str:
    creds = cts_farm_credentials(farm)
    return (creds or {}).get("holding") or ""


def _breed_label(
    *candidates: str | None,
    cbrd: int | float | None = None,
) -> str:
    """Prefer CBRD→BCMS mapping; fall back to an existing letter breed code."""
    mapped = bcms_breed_from_cbrd(cbrd)
    if mapped:
        return mapped
    for raw in candidates:
        text = (raw or "").strip().upper()
        if not text or text.isdigit():
            continue
        if text in {"HOLSTEIN", "BEEF"}:
            continue
        return text
    return ""


def _uk_calendar_date(when: dt.datetime | None) -> dt.date | None:
    """Interpret naive datetimes as UTC (how reported_at is stored)."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(_UK).date()


def _reported_keys(db: Session, farm: str) -> set[tuple[str, str, dt.date]]:
    rows = db.execute(
        select(
            CtsReportedMovement.movement_type,
            CtsReportedMovement.etag,
            CtsReportedMovement.event_date,
        ).where(
            CtsReportedMovement.farm == farm.upper(),
            CtsReportedMovement.status.in_(_SUPPRESS_PENDING_STATUSES),
        )
    ).all()
    return {
        (movement_type, etag, event_date)
        for movement_type, etag, event_date in rows
        if movement_type and etag and event_date
    }


def _still_awaiting_cts(
    *,
    movement_type: str,
    etag: str,
    on_cts: set[str],
) -> bool:
    if movement_type in {"birth", "move_on"}:
        return etag not in on_cts
    if movement_type in {"sale", "death"}:
        return etag in on_cts
    return False


def archive_confirmed_movements(db: Session, farm: str) -> dict[str, Any]:
    """Move accepted sends into Archive once CTS holding reflects them.

    - birth / move_on: ear tag now present on ``cts_on_holding``
    - sale / death: ear tag no longer present on ``cts_on_holding``
    """
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(f"Unsupported farm: {farm}")

    on_cts = _cts_etag_set(db, farm_key)
    reported_rows = db.scalars(
        select(CtsReportedMovement).where(
            CtsReportedMovement.farm == farm_key,
            CtsReportedMovement.status.in_(_ACTIVE_REPORT_STATUSES),
        )
    ).all()

    archived: list[dict[str, Any]] = []
    for reported in reported_rows:
        movement_type = (reported.movement_type or "").strip().lower()
        etag = normalize_cts_etag(reported.etag)
        if not movement_type or not etag or reported.event_date is None:
            continue
        if _still_awaiting_cts(
            movement_type=movement_type, etag=etag, on_cts=on_cts
        ):
            continue
        reported.status = "archived"
        reported.error_message = _ARCHIVED_MESSAGE
        archived.append(
            {
                "movement_type": movement_type,
                "etag": etag,
                "event_date": reported.event_date.isoformat(),
                "reported_at": (
                    reported.reported_at.isoformat(sep=" ", timespec="minutes")
                    if reported.reported_at is not None
                    else None
                ),
            }
        )

    if archived:
        db.commit()
        logger.info(
            "Archived %s CTS-confirmed movement(s) for %s",
            len(archived),
            farm_key,
        )
    return {
        "farm": farm_key,
        "archived_count": len(archived),
        "archived": archived,
    }


def requeue_stale_awaiting_movements(
    db: Session,
    farm: str,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Mark stale accepted sends as failed so they return to Pending.

    After a successful CTS cattle-on-holding sync, any birth/move-on still missing
    from the snapshot (or sale/death still present) whose UK send date is before
    today is treated as failed. Status becomes ``failed``, which removes the row
    from Awaiting CTS and from the reported suppress-list used by Pending.
    """
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(f"Unsupported farm: {farm}")

    today_uk = as_of or dt.datetime.now(_UK).date()
    on_cts = _cts_etag_set(db, farm_key)
    reported_rows = db.scalars(
        select(CtsReportedMovement).where(
            CtsReportedMovement.farm == farm_key,
            CtsReportedMovement.status.in_(_ACTIVE_REPORT_STATUSES),
        )
    ).all()

    requeued: list[dict[str, Any]] = []
    for reported in reported_rows:
        movement_type = (reported.movement_type or "").strip().lower()
        etag = normalize_cts_etag(reported.etag)
        if not movement_type or not etag or reported.event_date is None:
            continue
        if not _still_awaiting_cts(
            movement_type=movement_type, etag=etag, on_cts=on_cts
        ):
            continue
        sent_uk = _uk_calendar_date(reported.reported_at)
        if sent_uk is None or sent_uk >= today_uk:
            # Same UK calendar day as send — give CTS the overnight refresh.
            continue
        reported.status = "failed"
        reported.error_message = _STALE_AWAITING_MESSAGE
        requeued.append(
            {
                "movement_type": movement_type,
                "etag": etag,
                "event_date": reported.event_date.isoformat(),
                "reported_at": (
                    reported.reported_at.isoformat(sep=" ", timespec="minutes")
                    if reported.reported_at is not None
                    else None
                ),
            }
        )

    if requeued:
        db.commit()
        logger.info(
            "Requeued %s stale awaiting CTS movement(s) for %s",
            len(requeued),
            farm_key,
        )
    return {"farm": farm_key, "requeued_count": len(requeued), "requeued": requeued}


def _cts_etag_set(db: Session, farm: str) -> set[str]:
    return set(
        db.scalars(
            select(CtsOnHolding.etag).where(CtsOnHolding.farm == farm.upper())
        ).all()
    )


def _inventory_by_etag(db: Session, farm: str) -> dict[str, HerdInventory]:
    out: dict[str, HerdInventory] = {}
    for row in db.scalars(
        select(HerdInventory).where(HerdInventory.farm == farm.upper())
    ):
        key = normalize_cts_etag(row.etag)
        if key:
            out.setdefault(key, row)
    return out


def _pedigree_tags_by_etag(db: Session, farm: str) -> dict[str, dict[str, str]]:
    """Dam/sire ear tags (DREG/SREG) from inventory, falling back to pedigree."""
    out: dict[str, dict[str, str]] = {}

    def _set(etag: str | None, *, dreg: str | None = None, sreg: str | None = None) -> None:
        key = normalize_cts_etag(etag)
        if not key:
            return
        slot = out.setdefault(key, {})
        dreg_val = (dreg or "").strip()
        sreg_val = (sreg or "").strip()
        if dreg_val and "dreg" not in slot:
            slot["dreg"] = dreg_val
        if sreg_val and "sreg" not in slot:
            slot["sreg"] = sreg_val

    for row in db.scalars(
        select(HerdInventory).where(HerdInventory.farm == farm.upper())
    ):
        _set(row.etag, dreg=row.dreg, sreg=row.sreg)
    for row in db.scalars(
        select(PedigreeRegistrationRecord).where(
            PedigreeRegistrationRecord.farm == farm.upper()
        )
    ):
        _set(row.etag, dreg=row.dreg, sreg=row.sreg)
    return out


def _exit_events_by_etag(db: Session, farm: str) -> dict[str, CowEvent]:
    """Latest original SOLD/DIED CowEvent per normalized etag.

    Uses DairyComp event type as stored (DIED stays DIED). Import no longer
    rewrites DIED+TB/OFS to SOLD; those remain deaths for BCMS.
    """
    rows = db.scalars(
        select(CowEvent)
        .where(
            CowEvent.farm == farm.upper(),
            CowEvent.event.in_(("SOLD", "DIED")),
            CowEvent.event_date.isnot(None),
        )
        .order_by(CowEvent.event_date.desc(), CowEvent.id.desc())
    ).all()
    out: dict[str, CowEvent] = {}
    for row in rows:
        key = normalize_cts_etag(row.etag)
        if not key or key in out:
            continue
        out[key] = row
    return out


def _age_days(dob: dt.date | None, *, as_of: dt.date | None = None) -> int | None:
    if dob is None:
        return None
    days = ((as_of or dt.date.today()) - dob).days
    return days if days >= 0 else None


def _row(
    *,
    movement_type: str,
    farm: str,
    etag: str,
    cow_id: str,
    event_date: dt.date | None,
    sex: str,
    breed: str,
    dob: dt.date | None = None,
    dreg: str = "",
    sreg: str = "",
    source: str = "",
) -> dict[str, Any] | None:
    if not etag or event_date is None:
        return None
    return {
        "id": f"{movement_type}:{farm}:{etag}:{event_date.isoformat()}",
        "movement_type": movement_type,
        "farm": farm,
        "etag": etag,
        "cow_id": cow_id or "",
        "event_date": event_date.isoformat(),
        "days_since_event": _age_days(event_date),
        "sex": (sex or "").strip().upper()[:1],
        "breed": (breed or "").strip(),
        "dob": dob.isoformat() if dob else None,
        "age_days": _age_days(dob),
        "dreg": (dreg or "").strip(),
        "sreg": (sreg or "").strip(),
        "source": source,
    }


def _births_by_etag(db: Session, farm: str) -> dict[str, HerdBirth]:
    out: dict[str, HerdBirth] = {}
    rows = db.scalars(
        select(HerdBirth)
        .where(HerdBirth.farm == farm.upper(), HerdBirth.bdat.isnot(None))
        .order_by(HerdBirth.bdat.desc())
    ).all()
    for row in rows:
        key = normalize_cts_etag(row.etag)
        if key and key not in out:
            out[key] = row
    return out


def _purchases_by_etag(db: Session, farm: str) -> dict[str, StockPurchaseAnimal]:
    out: dict[str, StockPurchaseAnimal] = {}
    rows = db.scalars(
        select(StockPurchaseAnimal)
        .where(StockPurchaseAnimal.farm == farm.upper())
        .order_by(StockPurchaseAnimal.edat.desc())
    ).all()
    for row in rows:
        key = normalize_cts_etag(row.etag)
        if key and key not in out:
            out[key] = row
    return out


def list_pending_movements(db: Session, farm: str) -> dict[str, Any]:
    """Pending BCMS queue from DairyComp inventory vs CTS reconcile.

    - Birth / move-on: in herd inventory, not on BCMS holding
    - Sale / death: on BCMS holding, not in herd inventory (with SOLD/DIED event)
    """
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(f"Unsupported farm: {farm}")

    holding = _holding_for(farm_key)
    reported = _reported_keys(db, farm_key)
    on_cts = _cts_etag_set(db, farm_key)
    inventory = _inventory_by_etag(db, farm_key)
    pedigree_tags = _pedigree_tags_by_etag(db, farm_key)
    exits = _exit_events_by_etag(db, farm_key)
    births = _births_by_etag(db, farm_key)
    purchases = _purchases_by_etag(db, farm_key)

    pending: list[dict[str, Any]] = []

    # Sales / deaths: on CTS, not in DairyComp inventory.
    cts_rows = db.scalars(
        select(CtsOnHolding).where(CtsOnHolding.farm == farm_key)
    ).all()
    for cts in cts_rows:
        etag = cts.etag
        if not etag or etag in inventory:
            continue
        exit_ev = exits.get(etag)
        if exit_ev is None or exit_ev.event_date is None:
            continue
        movement_type = "death" if exit_ev.event == "DIED" else "sale"
        key = (movement_type, etag, exit_ev.event_date)
        if key in reported:
            continue
        tags = pedigree_tags.get(etag) or {}
        row = _row(
            movement_type=movement_type,
            farm=farm_key,
            etag=etag,
            cow_id=exit_ev.cow_id or "",
            event_date=exit_ev.event_date,
            sex=cts.sex or (exit_ev.gndr or ""),
            breed=_breed_label(cts.breed, cbrd=exit_ev.cbrd),
            dob=cts.dob or exit_ev.bdat,
            dreg=tags.get("dreg", ""),
            sreg=tags.get("sreg", ""),
            source="cts_on_holding + cow_events",
        )
        if row:
            pending.append(row)

    # Births / move-ons: in DairyComp inventory, not on CTS.
    # Prefer inventory EDAT (EDAT != BDAT → move_on); fall back to stock purchases.
    for etag, inv in inventory.items():
        if etag in on_cts:
            continue
        purchase = purchases.get(etag)
        birth = births.get(etag)
        inv_move_on = _inventory_is_move_on(inv)
        if inv_move_on or purchase is not None:
            movement_type = "move_on"
            event_date = inv.edat if inv_move_on else None
            if event_date is None and purchase is not None:
                event_date = purchase.edat
            sex = inv.gender or (purchase.gndr if purchase is not None else "") or ""
            breed = _breed_label(
                inv.sbrd,
                cbrd=(
                    inv.cbrd
                    if inv.cbrd is not None
                    else (purchase.cbrd if purchase is not None else None)
                ),
            )
            dob = inv.bdat or (purchase.bdat if purchase is not None else None)
            source = (
                "inventory not on cts + inv edat"
                if inv_move_on
                else "inventory not on cts + purchase"
            )
        else:
            movement_type = "birth"
            event_date = (birth.bdat if birth is not None else None) or inv.bdat
            sex = (birth.gndr if birth is not None else None) or inv.gender or ""
            # Prefer current inventory CBRD (mapped to BCMS); birth CBRD only if INV has none.
            breed = _breed_label(
                inv.sbrd,
                cbrd=(
                    inv.cbrd
                    if inv.cbrd is not None
                    else (birth.cbrd if birth is not None else None)
                ),
            )
            dob = (birth.bdat if birth is not None else None) or inv.bdat
            source = "inventory not on cts"
        if event_date is None:
            continue
        key = (movement_type, etag, event_date)
        if key in reported:
            continue
        tags = pedigree_tags.get(etag) or {}
        row = _row(
            movement_type=movement_type,
            farm=farm_key,
            etag=etag,
            cow_id=inv.cow_id or "",
            event_date=event_date,
            sex=sex,
            breed=breed,
            dob=dob,
            dreg=(inv.dreg or "").strip() or tags.get("dreg", ""),
            sreg=(inv.sreg or "").strip() or tags.get("sreg", ""),
            source=source,
        )
        if row:
            pending.append(row)

    type_order = {name: idx for idx, name in enumerate(MOVEMENT_TYPES)}
    pending.sort(
        key=lambda r: (
            type_order.get(r["movement_type"], 99),
            r["event_date"] or "",
            r["etag"],
        )
    )

    counts = {name: 0 for name in MOVEMENT_TYPES}
    for row in pending:
        counts[row["movement_type"]] = counts.get(row["movement_type"], 0) + 1

    return {
        "farm": farm_key,
        "holding": holding,
        "counts": counts,
        "total": len(pending),
        "rows": pending,
    }


def _reported_list_context(db: Session, farm_key: str) -> dict[str, Any]:
    return {
        "on_cts": _cts_etag_set(db, farm_key),
        "inventory": _inventory_by_etag(db, farm_key),
        "pedigree_tags": _pedigree_tags_by_etag(db, farm_key),
        "exits": _exit_events_by_etag(db, farm_key),
        "births": _births_by_etag(db, farm_key),
        "purchases": _purchases_by_etag(db, farm_key),
        "cts_by_etag": {
            row.etag: row
            for row in db.scalars(
                select(CtsOnHolding).where(CtsOnHolding.farm == farm_key)
            ).all()
            if row.etag
        },
    }


def _display_row_for_reported(
    reported: CtsReportedMovement,
    *,
    farm_key: str,
    ctx: dict[str, Any],
    source: str,
) -> dict[str, Any] | None:
    movement_type = (reported.movement_type or "").strip().lower()
    etag = normalize_cts_etag(reported.etag)
    event_date = reported.event_date
    if not movement_type or not etag or event_date is None:
        return None

    inv = ctx["inventory"].get(etag)
    cts = ctx["cts_by_etag"].get(etag)
    tags = ctx["pedigree_tags"].get(etag) or {}
    exit_ev = ctx["exits"].get(etag)
    birth = ctx["births"].get(etag)
    purchase = ctx["purchases"].get(etag)

    if movement_type in {"sale", "death"}:
        cow_id = (exit_ev.cow_id if exit_ev is not None else "") or ""
        sex = (cts.sex if cts is not None else "") or (
            exit_ev.gndr if exit_ev is not None else ""
        )
        breed = _breed_label(
            cts.breed if cts is not None else None,
            cbrd=exit_ev.cbrd if exit_ev is not None else None,
        )
        dob = (cts.dob if cts is not None else None) or (
            exit_ev.bdat if exit_ev is not None else None
        )
        dreg = tags.get("dreg", "")
        sreg = tags.get("sreg", "")
    else:
        cow_id = (inv.cow_id if inv is not None else "") or ""
        if movement_type == "move_on" and (
            (inv is not None and _inventory_is_move_on(inv)) or purchase is not None
        ):
            sex = (
                (inv.gender if inv is not None else "")
                or (purchase.gndr if purchase is not None else "")
                or ""
            )
            breed = _breed_label(
                inv.sbrd if inv is not None else None,
                cbrd=(
                    inv.cbrd
                    if inv is not None and inv.cbrd is not None
                    else (purchase.cbrd if purchase is not None else None)
                ),
            )
            dob = (inv.bdat if inv is not None else None) or (
                purchase.bdat if purchase is not None else None
            )
        else:
            sex = (
                (birth.gndr if birth is not None else None)
                or (inv.gender if inv is not None else "")
                or ""
            )
            breed = _breed_label(
                inv.sbrd if inv is not None else None,
                cbrd=(
                    inv.cbrd
                    if inv is not None and inv.cbrd is not None
                    else (birth.cbrd if birth is not None else None)
                ),
            )
            dob = (birth.bdat if birth is not None else None) or (
                inv.bdat if inv is not None else None
            )
        dreg = (
            (inv.dreg if inv is not None else "") or ""
        ).strip() or tags.get("dreg", "")
        sreg = (
            (inv.sreg if inv is not None else "") or ""
        ).strip() or tags.get("sreg", "")

    row = _row(
        movement_type=movement_type,
        farm=farm_key,
        etag=etag,
        cow_id=cow_id,
        event_date=event_date,
        sex=sex,
        breed=breed,
        dob=dob,
        dreg=dreg,
        sreg=sreg,
        source=source,
    )
    if not row:
        return None
    row["receipt"] = (reported.receipt or "").strip()
    row["status"] = (reported.status or "").strip()
    reported_at = reported.reported_at
    if reported_at is not None:
        row["reported_at"] = reported_at.isoformat(sep=" ", timespec="minutes")
    else:
        row["reported_at"] = None
    return row


def _sort_and_count_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    type_order = {name: idx for idx, name in enumerate(MOVEMENT_TYPES)}
    rows.sort(
        key=lambda r: (
            type_order.get(r["movement_type"], 99),
            r["event_date"] or "",
            r["etag"],
        )
    )
    counts = {name: 0 for name in MOVEMENT_TYPES}
    for row in rows:
        counts[row["movement_type"]] = counts.get(row["movement_type"], 0) + 1
    return rows, counts


def list_awaiting_cts_movements(db: Session, farm: str) -> dict[str, Any]:
    """Accepted BCMS submissions not yet reflected in the CTS holding snapshot.

    CTS cattle-on-holding often lags until the next overnight refresh, so:
    - birth / move_on: accepted, but ear tag still missing from ``cts_on_holding``
    - sale / death: accepted, but ear tag still present on ``cts_on_holding``

    Confirmed rows are promoted to Archive before this list is built.
    """
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(f"Unsupported farm: {farm}")

    archive_confirmed_movements(db, farm_key)

    holding = _holding_for(farm_key)
    ctx = _reported_list_context(db, farm_key)
    reported_rows = db.scalars(
        select(CtsReportedMovement)
        .where(
            CtsReportedMovement.farm == farm_key,
            CtsReportedMovement.status.in_(_ACTIVE_REPORT_STATUSES),
        )
        .order_by(CtsReportedMovement.reported_at.desc())
    ).all()

    awaiting: list[dict[str, Any]] = []
    for reported in reported_rows:
        movement_type = (reported.movement_type or "").strip().lower()
        etag = normalize_cts_etag(reported.etag)
        if not movement_type or not etag or reported.event_date is None:
            continue
        if not _still_awaiting_cts(
            movement_type=movement_type, etag=etag, on_cts=ctx["on_cts"]
        ):
            continue
        row = _display_row_for_reported(
            reported,
            farm_key=farm_key,
            ctx=ctx,
            source="accepted awaiting cts refresh",
        )
        if row:
            awaiting.append(row)

    awaiting, counts = _sort_and_count_rows(awaiting)
    return {
        "farm": farm_key,
        "holding": holding,
        "counts": counts,
        "total": len(awaiting),
        "rows": awaiting,
    }


def list_archived_movements(db: Session, farm: str) -> dict[str, Any]:
    """BCMS submissions confirmed against the CTS cattle-on-holding snapshot."""
    farm_key = farm.upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(f"Unsupported farm: {farm}")

    archive_confirmed_movements(db, farm_key)

    holding = _holding_for(farm_key)
    ctx = _reported_list_context(db, farm_key)
    reported_rows = db.scalars(
        select(CtsReportedMovement)
        .where(
            CtsReportedMovement.farm == farm_key,
            CtsReportedMovement.status == "archived",
        )
        .order_by(CtsReportedMovement.reported_at.desc())
    ).all()

    archived: list[dict[str, Any]] = []
    for reported in reported_rows:
        row = _display_row_for_reported(
            reported,
            farm_key=farm_key,
            ctx=ctx,
            source="archived confirmed on cts",
        )
        if row:
            archived.append(row)

    archived, counts = _sort_and_count_rows(archived)
    return {
        "farm": farm_key,
        "holding": holding,
        "counts": counts,
        "total": len(archived),
        "rows": archived,
    }


def mark_movements_reported(
    db: Session,
    *,
    farm: str,
    items: list[dict[str, Any]],
    status: str = "sent",
    receipt: str | None = None,
    error_message: str | None = None,
) -> int:
    """Record rows as reported so they drop out of the pending queue."""
    farm_key = farm.upper()
    added = 0
    for item in items:
        movement_type = str(item.get("movement_type") or "").strip().lower()
        etag = normalize_cts_etag(item.get("etag"))
        raw_date = item.get("event_date")
        if not movement_type or not etag or not raw_date:
            continue
        if isinstance(raw_date, dt.date):
            event_date = raw_date
        else:
            event_date = dt.date.fromisoformat(str(raw_date)[:10])
        existing = db.scalar(
            select(CtsReportedMovement).where(
                CtsReportedMovement.farm == farm_key,
                CtsReportedMovement.movement_type == movement_type,
                CtsReportedMovement.etag == etag,
                CtsReportedMovement.event_date == event_date,
            )
        )
        if existing is not None:
            existing.status = status
            existing.receipt = receipt
            existing.error_message = error_message
            existing.reported_at = dt.datetime.now(dt.timezone.utc).replace(
                tzinfo=None
            )
            continue
        db.add(
            CtsReportedMovement(
                farm=farm_key,
                movement_type=movement_type,
                etag=etag,
                event_date=event_date,
                status=status,
                receipt=receipt,
                error_message=error_message,
            )
        )
        added += 1
    db.commit()
    return added
