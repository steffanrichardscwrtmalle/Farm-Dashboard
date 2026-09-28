"""Derive purchased animals from cow events and current inventory (EDAT != BDAT)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    CowEvent,
    HerdInventory,
    StockPurchaseAnimal,
)
from app.services.herd_import_utils import CATEGORY_DAIRY, category_from_birth

GAD_FARM = "GAD"
GAD_PURCHASE_ETAG_PREFIX = "UK752261"
GAD_PURCHASE_EDAT_CUTOFF = dt.date(2025, 4, 1)
GAD_EXCLUDED_ETAGS: frozenset[str] = frozenset({"UK240837106626"})


def is_excluded_gad_purchase(farm: str, etag: str, edat: dt.date) -> bool:
    """GAD animals that are never purchases, or home-born UK752261* before Apr-2025."""
    if farm != GAD_FARM:
        return False
    if etag in GAD_EXCLUDED_ETAGS:
        return True
    return etag.startswith(GAD_PURCHASE_ETAG_PREFIX) and edat < GAD_PURCHASE_EDAT_CUTOFF


def classify_purchase_stock_group(
    lact: int | None,
    cbrd: int | None,
    gndr: str | None,
    *,
    edat: dt.date | None = None,
    fdat: dt.date | None = None,
    min_lact: int | None = None,
) -> str:
    """Classify a purchased animal using status at arrival, not current lactation."""
    if category_from_birth(cbrd, gndr) != CATEGORY_DAIRY:
        return STOCK_GROUP_BEEF

    arrival_lact = min_lact if min_lact is not None else lact
    if arrival_lact == 0:
        return STOCK_GROUP_YOUNGSTOCK

    # First freshening on this farm on/after arrival => purchased heifer (even if the
    # export's first row is already FRESH at lact=1).
    if (
        lact == 1
        and edat is not None
        and fdat is not None
        and fdat >= edat
    ):
        return STOCK_GROUP_YOUNGSTOCK

    if lact is not None and lact > 0:
        return STOCK_GROUP_COWS
    return STOCK_GROUP_YOUNGSTOCK


def _purchase_event_filter():
    return and_(
        CowEvent.etag.isnot(None),
        CowEvent.edat.isnot(None),
        CowEvent.bdat.isnot(None),
        CowEvent.edat != CowEvent.bdat,
    )


def _fetch_purchase_source_rows(db: Session) -> list[CowEvent]:
    purchase_filter = _purchase_event_filter()

    first_event = (
        select(
            CowEvent.farm,
            CowEvent.etag,
            func.min(CowEvent.event_date).label("first_event_date"),
        )
        .where(purchase_filter)
        .group_by(CowEvent.farm, CowEvent.etag)
        .subquery("first_event")
    )

    first_row = (
        select(func.min(CowEvent.id).label("event_id"))
        .join(
            first_event,
            and_(
                CowEvent.farm == first_event.c.farm,
                CowEvent.etag == first_event.c.etag,
                CowEvent.event_date == first_event.c.first_event_date,
            ),
        )
        .where(purchase_filter)
        .group_by(CowEvent.farm, CowEvent.etag)
        .subquery("first_row")
    )

    return list(
        db.scalars(
            select(CowEvent).where(CowEvent.id.in_(select(first_row.c.event_id)))
        ).all()
    )


def _as_int(value: int | float | None) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(number)


def _gender_code(value: str | None) -> str | None:
    """Inventory stores Female/Male; purchase rows keep the F/M code used by events."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text in {"F", "FEMALE"}:
        return "F"
    if text in {"M", "MALE"}:
        return "M"
    return text


def _purchase_mapping(
    *,
    farm: str,
    etag: str,
    edat: dt.date,
    bdat: dt.date,
    lact: int | None,
    cbrd: int | None,
    gndr: str | None,
    fdat: dt.date | None,
    min_lact: int | None,
    import_time: dt.datetime,
) -> dict[str, Any]:
    return {
        "farm": farm,
        "etag": etag,
        "edat": edat,
        "bdat": bdat,
        "lact": lact,
        "cbrd": cbrd,
        "gndr": gndr,
        "stock_group": classify_purchase_stock_group(
            lact,
            cbrd,
            gndr,
            edat=edat,
            fdat=fdat,
            min_lact=min_lact,
        ),
        "import_timestamp": import_time,
    }


def _identity(mapping: dict[str, Any]) -> tuple[Any, ...]:
    return (
        mapping["farm"],
        mapping["etag"],
        mapping["edat"],
        mapping["bdat"],
        mapping["lact"],
        mapping["cbrd"],
        mapping["gndr"],
        mapping["stock_group"],
    )


def _fetch_min_lact_by_purchase(db: Session) -> dict[tuple[str, str], int | None]:
    rows = db.execute(
        select(
            CowEvent.farm,
            CowEvent.etag,
            func.min(CowEvent.lact),
        )
        .where(_purchase_event_filter())
        .group_by(CowEvent.farm, CowEvent.etag)
    ).all()
    return {
        (str(farm).strip(), str(etag).strip()): (
            int(min_lact) if min_lact is not None else None
        )
        for farm, etag, min_lact in rows
        if farm and etag
    }


def _fetch_inventory_purchase_rows(db: Session) -> list[HerdInventory]:
    """Current animals whose entry date differs from birth date."""
    purchase_filter = and_(
        HerdInventory.etag.isnot(None),
        HerdInventory.edat.isnot(None),
        HerdInventory.bdat.isnot(None),
        HerdInventory.edat != HerdInventory.bdat,
    )
    first_row = (
        select(func.min(HerdInventory.id).label("inventory_id"))
        .where(purchase_filter)
        .group_by(HerdInventory.farm, HerdInventory.etag)
        .subquery("inventory_purchase")
    )
    return list(
        db.scalars(
            select(HerdInventory).where(
                HerdInventory.id.in_(select(first_row.c.inventory_id))
            )
        ).all()
    )


def _purchases_changed(db: Session, mappings: list[dict[str, Any]]) -> bool:
    existing = {
        (
            row.farm,
            row.etag,
            row.edat,
            row.bdat,
            row.lact,
            row.cbrd,
            row.gndr,
            row.stock_group,
        )
        for row in db.scalars(select(StockPurchaseAnimal)).all()
    }
    return existing != {_identity(mapping) for mapping in mappings}


def rebuild_stock_purchases(db: Session) -> dict[str, Any]:
    """Replace derived purchase animals from cow events and current inventory.

    Events keep animals that have left the herd. Inventory adds bought-in animals
    that are still on farm but have no event row. Where both exist, the event
    row wins so arrival lactation is used.
    """
    import_time = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    source_rows = _fetch_purchase_source_rows(db)
    min_lact_by_animal = _fetch_min_lact_by_purchase(db)

    mappings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    by_farm: dict[str, int] = {}
    by_stock_group: dict[str, int] = {}
    excluded_count = 0
    from_events = 0
    from_inventory = 0

    def _add(mapping: dict[str, Any], *, source: str) -> None:
        nonlocal from_events, from_inventory
        mappings.append(mapping)
        farm = mapping["farm"]
        stock_group = mapping["stock_group"]
        by_farm[farm] = by_farm.get(farm, 0) + 1
        by_stock_group[stock_group] = by_stock_group.get(stock_group, 0) + 1
        if source == "events":
            from_events += 1
        else:
            from_inventory += 1

    for row in source_rows:
        farm = str(row.farm).strip()
        etag = str(row.etag).strip()
        if not farm or not etag or row.edat is None or row.bdat is None:
            continue
        seen.add((farm, etag))
        if is_excluded_gad_purchase(farm, etag, row.edat):
            excluded_count += 1
            continue
        _add(
            _purchase_mapping(
                farm=farm,
                etag=etag,
                edat=row.edat,
                bdat=row.bdat,
                lact=row.lact,
                cbrd=row.cbrd,
                gndr=row.gndr,
                fdat=row.fdat,
                min_lact=min_lact_by_animal.get((farm, etag)),
                import_time=import_time,
            ),
            source="events",
        )

    for row in _fetch_inventory_purchase_rows(db):
        farm = str(row.farm or "").strip()
        etag = str(row.etag or "").strip()
        if not farm or not etag or row.edat is None or row.bdat is None:
            continue
        if (farm, etag) in seen:
            continue
        seen.add((farm, etag))
        if is_excluded_gad_purchase(farm, etag, row.edat):
            excluded_count += 1
            continue
        lact = _as_int(row.lact)
        _add(
            _purchase_mapping(
                farm=farm,
                etag=etag,
                edat=row.edat,
                bdat=row.bdat,
                lact=lact,
                cbrd=_as_int(row.cbrd),
                gndr=_gender_code(row.gender),
                fdat=row.fdat,
                min_lact=lact,
                import_time=import_time,
            ),
            source="inventory",
        )

    changed = _purchases_changed(db, mappings)
    if changed:
        db.execute(delete(StockPurchaseAnimal))
        if mappings:
            db.bulk_insert_mappings(StockPurchaseAnimal, mappings)

    return {
        "rows_imported": len(mappings),
        "from_events": from_events,
        "from_inventory": from_inventory,
        "excluded_count": excluded_count,
        "changed": changed,
        "farm_counts": by_farm,
        "stock_group_counts": by_stock_group,
        "imported_at": import_time.isoformat(timespec="seconds") if changed else None,
    }
