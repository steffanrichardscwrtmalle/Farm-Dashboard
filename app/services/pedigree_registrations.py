"""Pedigree registration worklist for Office Admin."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import AppSetting, HerdInventory, PedigreeRegistrationRecord, User
from app.services.events_common import normalize_farms
from app.services.herd_import_utils import BEEF_CBREED_MIN

PEDIGREE_RECIPIENT_KEY = "pedigree_registration_recipient"
FEMALE_GENDER = "Female"


def _normalize_etag(value: str | None) -> str:
    return (value or "").strip()


def _row_to_dict(
    *,
    farm: str,
    cow_id: str | None,
    etag: str,
    bdat: dt.date | None,
    dreg: str | None,
    sreg: str | None,
    sid: str | None,
    registered_at: dt.datetime | None = None,
    emailed_to: str | None = None,
    emailed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    return {
        "farm": farm,
        "cow_id": (cow_id or "").strip(),
        "etag": etag,
        "bdat": bdat.isoformat() if bdat else "",
        "dreg": dreg or "",
        "sreg": sreg or "",
        "sid": sid or "",
        "registered_at": registered_at.isoformat() if registered_at else None,
        "emailed_to": emailed_to,
        "sent_at": emailed_at.isoformat() if emailed_at else "",
        "registration_key": {"farm": farm, "etag": etag},
    }


def _inventory_eligible_conditions():
    """Female dairy heifers (CBRD < 102) in current inventory."""
    return and_(
        HerdInventory.gender == FEMALE_GENDER,
        HerdInventory.cbrd.isnot(None),
        HerdInventory.cbrd < BEEF_CBREED_MIN,
    )


def _pedigree_match():
    return and_(
        PedigreeRegistrationRecord.farm == HerdInventory.farm,
        PedigreeRegistrationRecord.etag == HerdInventory.etag,
    )


def _sreg_is_present():
    """SREG counts as present when it is not null, blank, or a '-' placeholder."""
    trimmed = func.trim(PedigreeRegistrationRecord.sreg)
    return and_(
        PedigreeRegistrationRecord.sreg.isnot(None),
        trimmed != "",
        trimmed != "-",
    )


def list_pedigree_registrations(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    sreg: str = "with",
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "status": status}

    sreg_mode = (sreg or "with").strip().lower()
    if sreg_mode not in {"with", "without", "all"}:
        sreg_mode = "with"

    if status == "registered":
        query = (
            select(
                PedigreeRegistrationRecord.farm,
                func.coalesce(HerdInventory.cow_id, PedigreeRegistrationRecord.cow_id),
                PedigreeRegistrationRecord.etag,
                HerdInventory.bdat,
                PedigreeRegistrationRecord.dreg,
                PedigreeRegistrationRecord.sreg,
                PedigreeRegistrationRecord.sid,
                PedigreeRegistrationRecord.registered_at,
                PedigreeRegistrationRecord.emailed_to,
                PedigreeRegistrationRecord.emailed_at,
            )
            .select_from(PedigreeRegistrationRecord)
            .outerjoin(
                HerdInventory,
                and_(
                    HerdInventory.farm == PedigreeRegistrationRecord.farm,
                    HerdInventory.etag == PedigreeRegistrationRecord.etag,
                ),
            )
            .where(PedigreeRegistrationRecord.farm.in_(selected_farms))
            .where(
                or_(
                    PedigreeRegistrationRecord.registered_at.isnot(None),
                    PedigreeRegistrationRecord.ped == 1,
                )
            )
            .order_by(
                PedigreeRegistrationRecord.registered_at.desc(),
                PedigreeRegistrationRecord.etag.asc(),
            )
        )
    else:
        # Exclude calves aged 10 days or younger (keep age >= 11 days).
        # Animals with an unknown birth date are kept (age cannot be determined).
        age_cutoff = dt.date.today() - dt.timedelta(days=11)
        query = (
            select(
                HerdInventory.farm,
                HerdInventory.cow_id,
                HerdInventory.etag,
                HerdInventory.bdat,
                PedigreeRegistrationRecord.dreg,
                PedigreeRegistrationRecord.sreg,
                PedigreeRegistrationRecord.sid,
                PedigreeRegistrationRecord.registered_at,
                PedigreeRegistrationRecord.emailed_to,
                PedigreeRegistrationRecord.emailed_at,
            )
            .select_from(HerdInventory)
            .join(PedigreeRegistrationRecord, _pedigree_match())
            .where(HerdInventory.farm.in_(selected_farms))
            .where(_inventory_eligible_conditions())
            .where(HerdInventory.lact == 0)
            .where(PedigreeRegistrationRecord.ped == 0)
            .where(PedigreeRegistrationRecord.dped == 1)
            .where(PedigreeRegistrationRecord.registered_at.is_(None))
            .where(
                or_(
                    HerdInventory.bdat.is_(None),
                    HerdInventory.bdat <= age_cutoff,
                )
            )
            .order_by(HerdInventory.bdat.asc(), HerdInventory.etag.asc())
        )

    if sreg_mode == "with":
        query = query.where(_sreg_is_present())
    elif sreg_mode == "without":
        query = query.where(~_sreg_is_present())

    rows = []
    for (
        farm,
        cow_id,
        etag,
        bdat,
        dreg,
        sreg,
        sid,
        registered_at,
        emailed_to,
        emailed_at,
    ) in db.execute(query).all():
        etag_norm = _normalize_etag(etag)
        if not etag_norm:
            continue
        rows.append(
            _row_to_dict(
                farm=farm,
                cow_id=cow_id,
                etag=etag_norm,
                bdat=bdat,
                dreg=dreg,
                sreg=sreg,
                sid=sid,
                registered_at=registered_at,
                emailed_to=emailed_to,
                emailed_at=emailed_at,
            )
        )

    return {"rows": rows, "total": len(rows), "status": status}


def build_pedigree_csv(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "ETAG", "BDAT", "DREG", "SREG", "SID"])
    for row in rows:
        writer.writerow(
            [
                row.get("cow_id") or "",
                row.get("etag") or "",
                row.get("bdat") or "",
                row.get("dreg") or "",
                row.get("sreg") or "",
                row.get("sid") or "",
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def _parse_item(item: dict[str, Any]) -> tuple[str, str]:
    farm = item["farm"]
    etag = _normalize_etag(item.get("etag"))
    if not etag:
        raise ValueError("Each item must include an etag.")
    return farm, etag


def _load_records_for_items(
    db: Session,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str], PedigreeRegistrationRecord]:
    parsed = [_parse_item(item) for item in items]
    if not parsed:
        return {}

    conditions = [
        and_(
            PedigreeRegistrationRecord.farm == farm,
            PedigreeRegistrationRecord.etag == etag,
        )
        for farm, etag in parsed
    ]
    records = db.scalars(
        select(PedigreeRegistrationRecord).where(or_(*conditions))
    ).all()
    return {(record.farm, record.etag): record for record in records}


def mark_registered(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
    *,
    emailed_to: str,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    recipient = (emailed_to or "").strip()
    existing = _load_records_for_items(db, items)
    registered = 0

    for item in items:
        farm, etag = _parse_item(item)
        record = existing.get((farm, etag))
        if record is None:
            record = PedigreeRegistrationRecord(farm=farm, etag=etag)
            db.add(record)
            existing[(farm, etag)] = record
        record.registered_at = now
        record.registered_by_user_id = user.id
        record.emailed_to = recipient or None
        record.emailed_at = now if recipient else None
        registered += 1

    db.commit()
    return {"registered": registered}


def restore_registrations(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    existing = _load_records_for_items(db, items)
    restored = 0

    for item in items:
        farm, etag = _parse_item(item)
        record = existing.get((farm, etag))
        if record is None or record.registered_at is None:
            continue
        record.registered_at = None
        record.registered_by_user_id = user.id
        record.emailed_to = None
        record.emailed_at = None
        restored += 1

    db.commit()
    return {"restored": restored}


def get_recipient(db: Session) -> dict[str, str | None]:
    row = db.scalar(select(AppSetting).where(AppSetting.key == PEDIGREE_RECIPIENT_KEY))
    return {"recipient": row.value if row else None}


def set_recipient(db: Session, email: str) -> dict[str, str]:
    normalized = (email or "").strip()
    if not normalized:
        raise ValueError("Recipient email is required.")
    row = db.scalar(select(AppSetting).where(AppSetting.key == PEDIGREE_RECIPIENT_KEY))
    if row is None:
        row = AppSetting(key=PEDIGREE_RECIPIENT_KEY, value=normalized)
        db.add(row)
    else:
        row.value = normalized
    db.commit()
    return {"recipient": normalized}
