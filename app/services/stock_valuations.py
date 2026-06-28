"""Fiscal month-end stock valuations reconstructed from inventory and herd events."""

from __future__ import annotations

import datetime as dt
import gc
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    CowEvent,
    HERD_FARM_OPTIONS,
    HerdBirth,
    HerdInventory,
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    StockPurchaseAnimal,
    StockValuationSnapshot,
)
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _iter_month_starts,
    normalize_farms,
)
from app.services.inventory_valuation import CATEGORIES, METHODOLOGY_SUMMARY, compute_value
from app.services.stock_accruals import build_stock_accruals_report
from app.services.stock_group import (
    VALUATION_CATEGORY_BY_STOCK_GROUP,
    stock_group_from_birth,
    stock_group_from_event_fields,
    stock_group_from_inventory,
    valuation_category_from_stock_group,
)

_EXIT_EVENTS = ("SOLD", "DIED")
_JV_EVENTS = ("GAME", "PATHWAY")
_CATEGORY_PREFIX: dict[str, str] = {
    "Beef": "beef",
    "Dairy": "dairy",
    "Youngstock": "youngstock",
}

# Missing FRESH lact=1 in cow_events (corrupt DC305 export); keyed by ETAG.
_MANUAL_FRESH_HEIFER_DATES: dict[str, dt.date] = {
    "UK752261609397": dt.date(2024, 3, 15),
    "UK752261509354": dt.date(2024, 6, 22),
    "UK752261509403": dt.date(2024, 5, 11),
    "UK752261308883": dt.date(2024, 4, 12),
    "UK752261509641": dt.date(2024, 9, 28),
}


@dataclass
class EventSnapshot:
    event_date: dt.date
    lact: int | None
    cbrd: int | None
    gndr: str | None
    bdat: dt.date | None
    event: str | None = None
    seq: int = 0
    farm: str | None = None


@dataclass
class PurchaseRecord:
    edat: dt.date
    lact: int | None
    cbrd: int | None
    gndr: str | None
    stock_group: str


@dataclass
class AnimalProfile:
    farm: str
    etag: str
    cow_id: str
    bdat: dt.date | None = None
    inventory_lact: int | None = None
    inventory_sbrd: str | None = None
    inventory_category: str | None = None
    in_anchor_inventory: bool = False
    events: list[EventSnapshot] = field(default_factory=list)
    purchases: list[PurchaseRecord] = field(default_factory=list)
    birth_category: str | None = None
    birth_cbrd: int | None = None
    birth_gndr: str | None = None
    transfer_stint_start: dt.date | None = None

    def best_event_on_or_before(
        self,
        close_date: dt.date,
        *,
        since: dt.date | None = None,
    ) -> EventSnapshot | None:
        """Latest event on or before close_date; same-day ties broken by import row order."""
        best: EventSnapshot | None = None
        for snap in self.events:
            if snap.event_date > close_date:
                continue
            if since is not None and snap.event_date < since:
                continue
            if best is None:
                best = snap
                continue
            if snap.event_date > best.event_date or (
                snap.event_date == best.event_date and snap.seq > best.seq
            ):
                best = snap
        return best


def _last_exit_on_or_before(profile: AnimalProfile, close_date: dt.date) -> dt.date | None:
    last: dt.date | None = None
    for snap in profile.events:
        if snap.event not in _EXIT_EVENTS or snap.event_date > close_date:
            continue
        if snap.farm is not None and snap.farm != profile.farm:
            continue
        if last is None or snap.event_date > last:
            last = snap.event_date
    return last


def _events_for_membership(profile: AnimalProfile) -> list[EventSnapshot]:
    """Farm-scoped events for on-farm stint and exit logic."""
    return [
        snap
        for snap in profile.events
        if snap.farm is None or snap.farm == profile.farm
    ]


def _compute_transfer_stint_start(
    profile: AnimalProfile,
    *,
    farm_events_by_etag: dict[str, dict[str, list[EventSnapshot]]] | None = None,
) -> dt.date | None:
    """On-farm membership starts at purchase when an animal transferred after birth elsewhere."""
    if not profile.bdat or not profile.purchases or not profile.etag:
        return None
    transfer_purchase: PurchaseRecord | None = None
    for purchase in profile.purchases:
        if purchase.edat <= profile.bdat:
            continue
        if transfer_purchase is None or purchase.edat < transfer_purchase.edat:
            transfer_purchase = purchase
    if transfer_purchase is None:
        return None
    for snap in _events_for_membership(profile):
        if snap.event_date < transfer_purchase.edat:
            return None

    if farm_events_by_etag is not None:
        other_farm_events = False
        for farm, snaps in farm_events_by_etag.get(profile.etag, {}).items():
            if farm == profile.farm:
                continue
            if any(snap.event_date < transfer_purchase.edat for snap in snaps):
                other_farm_events = True
                break
        if not other_farm_events:
            return None

    return transfer_purchase.edat


def _effective_transfer_start(
    profile: AnimalProfile,
    close_date: dt.date,
) -> dt.date | None:
    """Cross-farm CM membership starts at purchase when the animal was raised elsewhere."""
    return profile.transfer_stint_start


def _animal_on_farm_at_close(profile: AnimalProfile, close_date: dt.date) -> bool:
    """True when the animal is on farm at month-end (excludes sales/deaths on close_date)."""
    transfer_start = _effective_transfer_start(profile, close_date)
    if transfer_start is not None and close_date < transfer_start:
        return False
    last_exit = _last_exit_on_or_before(profile, close_date)
    if last_exit is not None and last_exit == close_date:
        return False
    since, _ = _stint_at_close(profile, close_date)
    if since is not None and since <= close_date:
        return last_exit is None or last_exit < since
    if profile.bdat is not None and profile.bdat <= close_date:
        return last_exit is None or last_exit < profile.bdat
    return False


def _stint_at_close(
    profile: AnimalProfile,
    close_date: dt.date,
) -> tuple[dt.date | None, PurchaseRecord | None]:
    """Start of the on-farm period at close_date (after last exit, or birth)."""
    transfer_start = _effective_transfer_start(profile, close_date)
    if transfer_start is not None and transfer_start > close_date:
        return None, None
    if transfer_start is not None:
        last_exit = _last_exit_on_or_before(profile, close_date)
        stint_purchase: PurchaseRecord | None = None
        for purchase in profile.purchases:
            if purchase.edat == transfer_start:
                stint_purchase = purchase
                break
        if stint_purchase is None:
            for purchase in profile.purchases:
                if purchase.edat >= transfer_start:
                    stint_purchase = purchase
                    break
        if last_exit is not None and last_exit >= transfer_start:
            if last_exit == close_date:
                return None, None
            if last_exit < close_date:
                for purchase in profile.purchases:
                    if purchase.edat > last_exit and purchase.edat <= close_date:
                        if stint_purchase is None or purchase.edat > stint_purchase.edat:
                            stint_purchase = purchase
                if stint_purchase is not None:
                    return stint_purchase.edat, stint_purchase
                return None, None
        return transfer_start, stint_purchase

    last_exit = _last_exit_on_or_before(profile, close_date)
    stint_purchase = None
    for purchase in profile.purchases:
        if purchase.edat > close_date:
            continue
        if last_exit is not None and purchase.edat <= last_exit:
            continue
        if stint_purchase is None or purchase.edat > stint_purchase.edat:
            stint_purchase = purchase
    if stint_purchase is not None:
        return stint_purchase.edat, stint_purchase
    if profile.bdat is not None and profile.bdat <= close_date:
        return profile.bdat, None
    return None, None


def _manual_fresh_heifer_in_stint(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None,
) -> bool:
    """True when a known first-lact freshen date applies but FRESH is missing from events."""
    fresh_date = _MANUAL_FRESH_HEIFER_DATES.get(profile.etag)
    if fresh_date is None or fresh_date > close_date:
        return False
    if since is not None and fresh_date < since:
        return False
    return True


def _effective_lact_at_close(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None = None,
    stint_purchase: PurchaseRecord | None = None,
) -> int:
    """Lactation for stock-group rules; honours FRESH calvings like Stock Accruals."""
    lact = 0
    if stint_purchase is not None and stint_purchase.edat <= close_date:
        lact = max(lact, int(stint_purchase.lact or 0))
    for snap in profile.events:
        if snap.event_date > close_date:
            continue
        if since is not None and snap.event_date < since:
            continue
        if snap.event == "FRESH" and snap.lact == 1:
            lact = max(lact, 1)
    if _manual_fresh_heifer_in_stint(profile, close_date, since=since):
        lact = max(lact, 1)
    best = profile.best_event_on_or_before(close_date, since=since)
    if best is not None:
        lact = max(lact, int(best.lact or 0))
    return lact


def _had_fresh_heifer_in_stint(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None,
) -> bool:
    """True when a FRESH lact=1 calving occurred in the current on-farm stint."""
    if _manual_fresh_heifer_in_stint(profile, close_date, since=since):
        return True
    for snap in profile.events:
        if snap.event_date > close_date:
            continue
        if since is not None and snap.event_date < since:
            continue
        if snap.event == "FRESH" and snap.lact == 1:
            return True
    return False


def _had_any_fresh_in_stint(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None,
) -> bool:
    """True when any FRESH calving occurred in the current on-farm stint."""
    for snap in profile.events:
        if snap.event_date > close_date:
            continue
        if since is not None and snap.event_date < since:
            continue
        if snap.event == "FRESH":
            return True
    return False


def _stock_group_from_accruals_rules(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None,
    stint_purchase: PurchaseRecord | None,
    lact: int,
    snap: EventSnapshot | None,
) -> str:
    """Mirror Stock Accruals: cows need FRESH lact=1 or lact>1 / cow purchase."""
    if stint_purchase is not None and stint_purchase.stock_group == STOCK_GROUP_COWS:
        return STOCK_GROUP_COWS

    if stint_purchase is not None and stint_purchase.stock_group == STOCK_GROUP_YOUNGSTOCK:
        purchase_lact = int(stint_purchase.lact or 0)
        if purchase_lact == 0:
            if not _had_fresh_heifer_in_stint(profile, close_date, since=since):
                return STOCK_GROUP_YOUNGSTOCK
        elif not _had_any_fresh_in_stint(profile, close_date, since=since):
            return STOCK_GROUP_YOUNGSTOCK

    if snap is None:
        if stint_purchase is not None:
            return stint_purchase.stock_group
        if profile.birth_category is not None:
            return stock_group_from_birth(
                profile.birth_category, profile.birth_cbrd, profile.birth_gndr
            )
        return STOCK_GROUP_BEEF

    snap_group = stock_group_from_event_fields(lact, snap.cbrd, snap.gndr)
    if snap_group != STOCK_GROUP_COWS:
        return snap_group

    if _had_fresh_heifer_in_stint(profile, close_date, since=since):
        return STOCK_GROUP_COWS

    # Lact 1 without FRESH only stays youngstock when newly promoted this month
    # (e.g. ILL marking). Animals already lact>=1 at month open remain cows.
    if lact <= 1:
        month_open = _month_start(close_date)
        prior_close = month_open - dt.timedelta(days=1)
        if since is None or since <= prior_close:
            prior_lact = _effective_lact_at_close(
                profile,
                prior_close,
                since=since,
                stint_purchase=stint_purchase,
            )
            if prior_lact <= 0:
                return STOCK_GROUP_YOUNGSTOCK

    return STOCK_GROUP_COWS


def _normalize_key_part(value: str | None) -> str:
    return (value or "").strip()


def animal_key(farm: str, etag: str | None, cow_id: str | None) -> tuple[str, str]:
    farm_norm = _normalize_key_part(farm)
    etag_norm = _normalize_key_part(etag)
    if etag_norm:
        return farm_norm, f"etag:{etag_norm}"
    return farm_norm, f"id:{_normalize_key_part(cow_id)}"


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _month_end(month_start: dt.date) -> dt.date:
    if month_start.month == 12:
        next_month = dt.date(month_start.year + 1, 1, 1)
    else:
        next_month = dt.date(month_start.year, month_start.month + 1, 1)
    return next_month - dt.timedelta(days=1)


def _empty_category_totals() -> dict[str, dict[str, int | float]]:
    return {
        cat: {"count": 0, "value_gbp": 0.0, "aged_sum": 0, "lact_sum": 0.0, "lact_count": 0}
        for cat in CATEGORIES
    }


def _empty_farm_meta() -> dict[str, int]:
    return {"dairy_cows": 0}


def _earliest_event_after(profile: AnimalProfile, close_date: dt.date) -> EventSnapshot | None:
    for snap in profile.events:
        if snap.event_date > close_date:
            return snap
    return None


def _earliest_fresh_after(profile: AnimalProfile, close_date: dt.date) -> EventSnapshot | None:
    best: EventSnapshot | None = None
    for snap in profile.events:
        if snap.event_date <= close_date:
            continue
        if snap.event != "FRESH":
            continue
        if best is None or snap.event_date < best.event_date or (
            snap.event_date == best.event_date and snap.seq < best.seq
        ):
            best = snap
    return best


def _try_lact_from_first_future_fresh(
    profile: AnimalProfile,
    close_date: dt.date,
) -> tuple[int, str, dt.date | None] | None:
    """When event history starts after close, infer lact from first FRESH lact - 1."""
    if profile.best_event_on_or_before(close_date, since=None) is not None:
        return None
    fresh = _earliest_fresh_after(profile, close_date)
    if fresh is None:
        return None
    lact = max(0, int(fresh.lact or 0) - 1)
    cbrd = fresh.cbrd if fresh.cbrd is not None else profile.birth_cbrd
    gndr = fresh.gndr if fresh.gndr is not None else profile.birth_gndr
    if cbrd is None and gndr is None and profile.in_anchor_inventory:
        stock_group = stock_group_from_inventory(lact, profile.inventory_sbrd)
    else:
        stock_group = stock_group_from_event_fields(lact, cbrd, gndr)
    return lact, stock_group, fresh.bdat


def _resolve_state_at(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    anchor_date: dt.date | None = None,
) -> dict[str, Any] | None:
    bdat = profile.bdat

    if (
        anchor_date is not None
        and close_date == anchor_date
        and profile.in_anchor_inventory
    ):
        lact = profile.inventory_lact if profile.inventory_lact is not None else 0
        stock_group = stock_group_from_inventory(lact, profile.inventory_sbrd)
    else:
        since, stint_purchase = _stint_at_close(profile, close_date)
        snap = profile.best_event_on_or_before(close_date, since=since)
        if snap is not None:
            lact = _effective_lact_at_close(
                profile,
                close_date,
                since=since,
                stint_purchase=stint_purchase,
            )
            if snap.bdat is not None:
                bdat = snap.bdat
            stock_group = _stock_group_from_accruals_rules(
                profile,
                close_date,
                since=since,
                stint_purchase=stint_purchase,
                lact=lact,
                snap=snap,
            )
        elif stint_purchase is not None and stint_purchase.edat <= close_date:
            lact = int(stint_purchase.lact or 0)
            stock_group = stint_purchase.stock_group
        elif profile.in_anchor_inventory:
            inferred = _try_lact_from_first_future_fresh(profile, close_date)
            if inferred is not None:
                lact, stock_group, fresh_bdat = inferred
                if fresh_bdat is not None:
                    bdat = fresh_bdat
            else:
                lact = profile.inventory_lact if profile.inventory_lact is not None else 0
                stock_group = stock_group_from_inventory(lact, profile.inventory_sbrd)
        elif profile.birth_category is not None:
            lact = 0
            stock_group = stock_group_from_birth(
                profile.birth_category, profile.birth_cbrd, profile.birth_gndr
            )
        else:
            inferred = _try_lact_from_first_future_fresh(profile, close_date)
            if inferred is not None:
                lact, stock_group, fresh_bdat = inferred
                if fresh_bdat is not None:
                    bdat = fresh_bdat
            else:
                future = _earliest_event_after(profile, close_date)
                if future is None:
                    return None
                lact = future.lact if future.lact is not None else 0
                if future.bdat is not None:
                    bdat = future.bdat
                stock_group = stock_group_from_event_fields(lact, future.cbrd, future.gndr)

    if bdat is None:
        return None

    aged_days = (close_date - bdat).days
    if aged_days < 0:
        return None

    category = valuation_category_from_stock_group(stock_group)
    value = compute_value(lact, category, aged_days)
    return {
        "farm": profile.farm,
        "lact": lact,
        "stock_group": stock_group,
        "category": category,
        "aged_days": aged_days,
        "value": value,
    }


def _build_profiles(
    db: Session,
    *,
    selected_farms: list[str],
    anchor_ts: dt.datetime,
) -> tuple[
    dt.date,
    dict[tuple[str, str], AnimalProfile],
    set[tuple[str, str]],
    dict[tuple[str, str], dt.date],
    dict[tuple[str, str], dt.date],
    dict[tuple[str, str], dt.date],
]:
    anchor_date = anchor_ts.date()
    profiles: dict[tuple[str, str], AnimalProfile] = {}

    def get_profile(farm: str, etag: str | None, cow_id: str | None) -> AnimalProfile:
        key = animal_key(farm, etag, cow_id)
        if key not in profiles:
            profiles[key] = AnimalProfile(
                farm=key[0],
                etag=_normalize_key_part(etag),
                cow_id=_normalize_key_part(cow_id),
            )
        return profiles[key]

    inventory_rows = db.scalars(
        select(HerdInventory).where(
            HerdInventory.farm.in_(selected_farms),
            HerdInventory.import_timestamp == anchor_ts,
        )
    ).all()

    inventory_keys: set[tuple[str, str]] = set()
    for row in inventory_rows:
        key = animal_key(row.farm, row.etag, row.cow_id)
        inventory_keys.add(key)
        profile = get_profile(row.farm, row.etag, row.cow_id)
        profile.in_anchor_inventory = True
        profile.bdat = row.bdat or profile.bdat
        profile.inventory_lact = int(row.lact) if row.lact is not None else None
        profile.inventory_sbrd = row.sbrd
        profile.inventory_category = row.category

    exit_keys: dict[tuple[str, str], dt.date] = {}
    jv_keys: dict[tuple[str, str], dt.date] = {}
    jv_rows = db.execute(
        select(
            CowEvent.farm,
            CowEvent.etag,
            CowEvent.cow_id,
            CowEvent.event_date,
            CowEvent.bdat,
        )
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event.in_(_JV_EVENTS))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.event_date <= anchor_date)
    ).all()
    for farm, etag, cow_id, event_date, bdat in jv_rows:
        if event_date is None:
            continue
        key = animal_key(farm, etag, cow_id)
        profile = get_profile(farm, etag, cow_id)
        profile.bdat = bdat or profile.bdat
        prev = jv_keys.get(key)
        if prev is None or event_date < prev:
            jv_keys[key] = event_date

    entry_keys: dict[tuple[str, str], dt.date] = {}
    birth_rows = db.execute(
        select(
            HerdBirth.farm,
            HerdBirth.etag,
            HerdBirth.cow_id,
            HerdBirth.bdat,
            HerdBirth.category,
            HerdBirth.cbrd,
            HerdBirth.gndr,
        )
        .where(HerdBirth.farm.in_(selected_farms))
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.bdat <= anchor_date)
    ).all()
    for farm, etag, cow_id, bdat, category, cbrd, gndr in birth_rows:
        if bdat is None:
            continue
        key = animal_key(farm, etag, cow_id)
        profile = get_profile(farm, etag, cow_id)
        profile.bdat = bdat
        profile.birth_category = category
        profile.birth_cbrd = int(cbrd) if cbrd is not None else None
        profile.birth_gndr = gndr
        prev = entry_keys.get(key)
        if prev is None or bdat < prev:
            entry_keys[key] = bdat

    purchase_rows = db.scalars(
        select(StockPurchaseAnimal).where(
            StockPurchaseAnimal.farm.in_(selected_farms),
            StockPurchaseAnimal.edat <= anchor_date,
        )
    ).all()
    for row in purchase_rows:
        key = animal_key(row.farm, row.etag, None)
        profile = get_profile(row.farm, row.etag, None)
        profile.bdat = row.bdat or profile.bdat
        profile.purchases.append(
            PurchaseRecord(
                edat=row.edat,
                lact=int(row.lact) if row.lact is not None else None,
                cbrd=int(row.cbrd) if row.cbrd is not None else None,
                gndr=row.gndr,
                stock_group=row.stock_group,
            )
        )
        prev = entry_keys.get(key)
        if prev is None or row.edat < prev:
            entry_keys[key] = row.edat

    for profile in profiles.values():
        profile.purchases.sort(key=lambda purchase: purchase.edat)

    all_event_rows = db.execute(
        select(
            CowEvent.id,
            CowEvent.farm,
            CowEvent.etag,
            CowEvent.cow_id,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.lact,
            CowEvent.cbrd,
            CowEvent.gndr,
            CowEvent.bdat,
        )
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.event_date <= anchor_date)
        .order_by(CowEvent.event_date.asc(), CowEvent.id.asc())
        .execution_options(yield_per=5000)
    )
    for row_id, farm, etag, cow_id, event_name, event_date, lact, cbrd, gndr, bdat in all_event_rows:
        if event_date is None:
            continue
        key = animal_key(farm, etag, cow_id)
        profile = get_profile(farm, etag, cow_id)
        profile.bdat = bdat or profile.bdat
        profile.events.append(
            EventSnapshot(
                event_date=event_date,
                lact=int(lact) if lact is not None else None,
                cbrd=int(cbrd) if cbrd is not None else None,
                gndr=gndr,
                bdat=bdat,
                event=str(event_name) if event_name is not None else None,
                seq=int(row_id),
                farm=farm,
            )
        )
        if event_name in _EXIT_EVENTS:
            prev = exit_keys.get(key)
            if prev is None or event_date > prev:
                exit_keys[key] = event_date

    for profile in profiles.values():
        profile.events.sort(key=lambda snap: (snap.event_date, snap.seq))

    farm_events_by_etag: dict[str, dict[str, list[EventSnapshot]]] = {}
    for profile in profiles.values():
        if not profile.etag:
            continue
        farm_events_by_etag.setdefault(profile.etag, {})[profile.farm] = profile.events

    for profile in profiles.values():
        profile.transfer_stint_start = _compute_transfer_stint_start(
            profile,
            farm_events_by_etag=farm_events_by_etag,
        )

    etag_events: dict[str, list[EventSnapshot]] = {}
    for profile in profiles.values():
        if not profile.etag:
            continue
        etag_events.setdefault(profile.etag, []).extend(profile.events)

    for snaps in etag_events.values():
        snaps.sort(key=lambda snap: (snap.event_date, snap.seq))

    for profile in profiles.values():
        if not profile.etag:
            continue
        shared = etag_events.get(profile.etag)
        if shared is not None:
            profile.events = shared

    del farm_events_by_etag
    del etag_events

    return anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys


def _on_farm_keys(
    close_date: dt.date,
    anchor_date: dt.date,
    inventory_keys: set[tuple[str, str]],
    exit_keys: dict[tuple[str, str], dt.date],
    entry_keys: dict[tuple[str, str], dt.date],
    jv_keys: dict[tuple[str, str], dt.date],
    profiles: dict[tuple[str, str], AnimalProfile] | None = None,
) -> set[tuple[str, str]]:
    if close_date > anchor_date:
        return set()

    keys = set(inventory_keys)
    for key, jv_date in jv_keys.items():
        if jv_date <= close_date:
            keys.discard(key)
    for key, exit_date in exit_keys.items():
        if close_date < exit_date <= anchor_date:
            jv_date = jv_keys.get(key)
            if jv_date is not None and jv_date <= close_date:
                continue
            keys.add(key)
    for key, jv_date in jv_keys.items():
        if close_date < jv_date <= anchor_date:
            keys.add(key)
    for key, entry_date in entry_keys.items():
        if close_date < entry_date <= anchor_date:
            keys.discard(key)
    if profiles is not None:
        keys = {
            key
            for key in keys
            if key in profiles and _animal_on_farm_at_close(profiles[key], close_date)
        }
    return keys


def _aggregate_animals(
    profiles: dict[tuple[str, str], AnimalProfile],
    keys: set[tuple[str, str]],
    close_date: dt.date,
    *,
    anchor_date: dt.date | None = None,
) -> tuple[dict[str, dict[str, dict[str, int | float]]], dict[str, dict[str, int]]]:
    farms: dict[str, dict[str, dict[str, int | float]]] = {}
    farm_meta: dict[str, dict[str, int]] = {}
    for key in keys:
        profile = profiles.get(key)
        if profile is None:
            continue
        state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
        if state is None:
            continue
        farm = state["farm"]
        category = state["category"]
        farms.setdefault(farm, _empty_category_totals())
        farm_meta.setdefault(farm, _empty_farm_meta())
        bucket = farms[farm][category]
        bucket["count"] = int(bucket["count"]) + 1
        bucket["value_gbp"] = float(bucket["value_gbp"]) + float(state["value"])
        bucket["aged_sum"] = int(bucket["aged_sum"]) + int(state["aged_days"])
        lact = int(state["lact"])
        if state["stock_group"] == STOCK_GROUP_COWS:
            farm_meta[farm]["dairy_cows"] += 1
            bucket["lact_sum"] = float(bucket["lact_sum"]) + lact
            bucket["lact_count"] = int(bucket["lact_count"]) + 1
    return farms, farm_meta


def _farm_summary_row(
    farm_data: dict[str, dict[str, int | float]],
    meta: dict[str, int] | None = None,
) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    for cat in CATEGORIES:
        bucket = farm_data.get(cat, {})
        count = int(bucket.get("count", 0))
        value_gbp = float(bucket.get("value_gbp", 0))
        aged_sum = int(bucket.get("aged_sum", 0))
        lact_sum = float(bucket.get("lact_sum", 0))
        lact_count = int(bucket.get("lact_count", 0))
        categories[cat] = {
            "count": count,
            "value_gbp": round(value_gbp, 0),
            "avg_age_days": math.floor(aged_sum / count) if count else 0,
            "avg_value_gbp": math.floor(value_gbp / count) if count else 0,
            "avg_lact": round(lact_sum / lact_count, 1) if lact_count else None,
        }
    grand_total = sum(categories[cat]["value_gbp"] for cat in CATEGORIES)
    total_animals = sum(categories[cat]["count"] for cat in CATEGORIES)
    return {
        "categories": categories,
        "grand_total_gbp": round(grand_total, 0),
        "total_animals": total_animals,
        "dairy_cows": int((meta or {}).get("dairy_cows", 0)),
    }


def _jv_beef_still_on_farm_count(
    profiles: dict[tuple[str, str], AnimalProfile],
    jv_keys: dict[tuple[str, str], dt.date],
    exit_keys: dict[tuple[str, str], dt.date],
    close_date: dt.date,
    farm: str,
) -> int:
    """Beef animals with JV transfer on/before close_date that accruals still counts."""
    count = 0
    for key, jv_date in jv_keys.items():
        if jv_date > close_date:
            continue
        exit_date = exit_keys.get(key)
        if exit_date is not None and exit_date <= close_date:
            continue
        profile = profiles.get(key)
        if profile is None or profile.farm != farm:
            continue
        state = _resolve_state_at(profile, jv_date)
        if state is not None and state["stock_group"] == STOCK_GROUP_BEEF:
            count += 1
    return count


def _accruals_closing_for_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_start: dt.date,
    fiscal_year: int,
) -> int | None:
    month_end = min(_month_end(month_start), _fiscal_year_calendar_bounds(fiscal_year)[1])
    report = build_stock_accruals_report(
        db,
        farms=[farm],
        stock_group=stock_group,
        fiscal_year=fiscal_year,
        month_from=month_start,
        month_to=month_end,
    )
    for row in report.get("rows", []):
        if row.get("month_start") == month_start.isoformat():
            return int(row["closing"])
    return None


def _valuation_count_for_stock_group(
    month_totals: dict[str, Any],
    farm: str,
    stock_group: str,
) -> int:
    category = VALUATION_CATEGORY_BY_STOCK_GROUP[stock_group]
    farm_totals = month_totals.get(farm, {})
    if stock_group == STOCK_GROUP_COWS:
        return int(farm_totals.get("dairy_cows", 0))
    return int(farm_totals.get("categories", {}).get(category, {}).get("count", 0))


def _comparison_rows_from_val_months(
    db: Session,
    *,
    selected_farms: list[str],
    fiscal_year: int,
    val_months: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    if anchor_ts is None or not selected_farms or not val_months:
        return []

    _, profiles, _, exit_keys, _, jv_keys = _build_profiles(
        db,
        selected_farms=list(HERD_FARM_OPTIONS),
        anchor_ts=anchor_ts,
    )

    stock_groups = (
        (STOCK_GROUP_COWS, "cows"),
        (STOCK_GROUP_YOUNGSTOCK, "youngstock"),
        (STOCK_GROUP_BEEF, "beef"),
    )
    rows: list[dict[str, Any]] = []

    for month_row in val_months:
        month_start = dt.date.fromisoformat(month_row["month_start"])
        close_date = dt.date.fromisoformat(month_row["close_date"])
        totals = month_row.get("totals", {})

        for farm in selected_farms:
            if farm not in totals:
                continue
            jv_beef = _jv_beef_still_on_farm_count(
                profiles, jv_keys, exit_keys, close_date, farm
            )
            for stock_group, label in stock_groups:
                acc_closing = _accruals_closing_for_month(
                    db,
                    farm=farm,
                    stock_group=stock_group,
                    month_start=month_start,
                    fiscal_year=fiscal_year,
                )
                if acc_closing is None:
                    continue
                expected = acc_closing
                if stock_group == STOCK_GROUP_BEEF:
                    expected = max(0, acc_closing - jv_beef)
                val_count = _valuation_count_for_stock_group(totals, farm, stock_group)
                delta = val_count - expected
                rows.append(
                    {
                        "month_start": month_start.isoformat(),
                        "farm": farm,
                        "stock_group": label,
                        "accruals_closing": acc_closing,
                        "jv_beef_adjustment": jv_beef if stock_group == STOCK_GROUP_BEEF else 0,
                        "expected": expected,
                        "valuation_count": val_count,
                        "delta": delta,
                        "matched": delta == 0,
                    }
                )

    return rows


def compare_valuations_to_accruals(
    db: Session,
    *,
    farms: list[str] | None = None,
    fiscal_year: int,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
) -> dict[str, Any]:
    """Compare reconstruction headcounts to Stock Accruals closing (beef net of JV)."""
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "mismatches": 0}

    val_report = build_stock_valuations_report(
        db,
        farms=selected_farms,
        fiscal_year=fiscal_year,
        month_from=month_from,
        month_to=month_to,
    )
    rows = _comparison_rows_from_val_months(
        db,
        selected_farms=selected_farms,
        fiscal_year=fiscal_year,
        val_months=val_report.get("months", []),
    )
    mismatches = sum(1 for row in rows if not row["matched"])

    return {
        "anchor_date": val_report.get("anchor_date"),
        "fiscal_year": fiscal_year,
        "rows": rows,
        "mismatches": mismatches,
    }


def _build_kpi_detail(
    month_totals: dict[str, Any],
) -> dict[str, Any]:
    all_data = month_totals.get("all", {})
    all_cats = all_data.get("categories", {})
    summary_rows = []
    for cat in CATEGORIES:
        cm_val = month_totals.get("CM", {}).get("categories", {}).get(cat, {}).get("value_gbp", 0)
        gad_val = month_totals.get("GAD", {}).get("categories", {}).get(cat, {}).get("value_gbp", 0)
        summary_rows.append(
            {
                "category": cat,
                "cm_gbp": cm_val,
                "gad_gbp": gad_val,
                "total_gbp": round(float(cm_val) + float(gad_val), 0),
            }
        )
    grand_cm = sum(row["cm_gbp"] for row in summary_rows)
    grand_gad = sum(row["gad_gbp"] for row in summary_rows)
    summary_rows.append(
        {
            "category": "Total",
            "cm_gbp": round(grand_cm, 0),
            "gad_gbp": round(grand_gad, 0),
            "total_gbp": round(grand_cm + grand_gad, 0),
        }
    )
    return {
        "total_cows": all_data.get("dairy_cows", 0),
        "total_youngstock": all_cats.get("Youngstock", {}).get("count", 0),
        "total_beef": all_cats.get("Beef", {}).get("count", 0),
        "total_animals": all_data.get("total_animals", 0),
        "sum_value_gbp": all_data.get("grand_total_gbp", 0),
        "farms": {
            farm: month_totals.get(farm, {})
            for farm in ("CM", "GAD")
            if farm in month_totals
        },
        "summary_table": summary_rows,
    }


def _fiscal_year_options(db: Session, selected_farms: list[str]) -> list[int]:
    years: set[int] = set()
    for value in db.scalars(
        select(CowEvent.fiscal_year)
        .where(CowEvent.fiscal_year.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .distinct()
    ).all():
        if value is not None:
            years.add(int(value))
    for value in db.scalars(
        select(HerdBirth.fiscal_year)
        .where(HerdBirth.fiscal_year.isnot(None))
        .where(HerdBirth.farm.in_(selected_farms))
        .distinct()
    ).all():
        if value is not None:
            years.add(int(value))
    anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    if anchor_ts is not None:
        month = anchor_ts.month
        year = anchor_ts.year
        years.add(year + 1 if month >= 4 else year)
    return sorted(years, reverse=True)


def _snapshot_has_data(db: Session, anchor_ts: dt.datetime) -> bool:
    row = db.scalar(
        select(func.count())
        .select_from(StockValuationSnapshot)
        .where(StockValuationSnapshot.anchor_import_timestamp == anchor_ts)
    )
    return bool(row)


def _farm_data_from_snapshot(snapshot: StockValuationSnapshot) -> tuple[
    dict[str, dict[str, int | float]], dict[str, int]
]:
    farm_data = _empty_category_totals()
    for category, prefix in _CATEGORY_PREFIX.items():
        farm_data[category] = {
            "count": int(getattr(snapshot, f"{prefix}_count")),
            "value_gbp": float(getattr(snapshot, f"{prefix}_value_gbp")),
            "aged_sum": int(getattr(snapshot, f"{prefix}_aged_sum")),
            "lact_sum": float(getattr(snapshot, f"{prefix}_lact_sum")),
            "lact_count": int(getattr(snapshot, f"{prefix}_lact_count")),
        }
    return farm_data, {"dairy_cows": int(snapshot.dairy_cows)}


def _snapshot_from_farm_month(
    *,
    anchor_ts: dt.datetime,
    farm: str,
    month_start: dt.date,
    close_date: dt.date,
    farm_data: dict[str, dict[str, int | float]],
    farm_meta: dict[str, int],
) -> StockValuationSnapshot:
    snapshot = StockValuationSnapshot(
        anchor_import_timestamp=anchor_ts,
        farm=farm,
        month_start=month_start,
        close_date=close_date,
        dairy_cows=int(farm_meta.get("dairy_cows", 0)),
    )
    for category, prefix in _CATEGORY_PREFIX.items():
        bucket = farm_data.get(category, {})
        setattr(snapshot, f"{prefix}_count", int(bucket.get("count", 0)))
        setattr(snapshot, f"{prefix}_value_gbp", float(bucket.get("value_gbp", 0)))
        setattr(snapshot, f"{prefix}_aged_sum", int(bucket.get("aged_sum", 0)))
        setattr(snapshot, f"{prefix}_lact_sum", float(bucket.get("lact_sum", 0)))
        setattr(snapshot, f"{prefix}_lact_count", int(bucket.get("lact_count", 0)))
    return snapshot


def _compute_month_rows(
    *,
    month_starts: list[dt.date],
    anchor_date: dt.date,
    selected_farms: list[str],
    profiles: dict[tuple[str, str], AnimalProfile],
    inventory_keys: set[tuple[str, str]],
    exit_keys: dict[tuple[str, str], dt.date],
    entry_keys: dict[tuple[str, str], dt.date],
    jv_keys: dict[tuple[str, str], dt.date],
) -> list[dict[str, Any]]:
    months: list[dict[str, Any]] = []
    for month_start in month_starts:
        close_date = min(_month_end(month_start), anchor_date)
        if close_date < month_start:
            continue
        keys = _on_farm_keys(
            close_date,
            anchor_date,
            inventory_keys,
            exit_keys,
            entry_keys,
            jv_keys,
            profiles,
        )
        farm_agg, farm_meta = _aggregate_animals(
            profiles, keys, close_date, anchor_date=anchor_date
        )

        totals: dict[str, Any] = {}
        for farm in selected_farms:
            totals[farm] = _farm_summary_row(
                farm_agg.get(farm, _empty_category_totals()),
                farm_meta.get(farm, _empty_farm_meta()),
            )

        all_cats = _empty_category_totals()
        all_meta = _empty_farm_meta()
        for farm in selected_farms:
            for cat in CATEGORIES:
                for field_name in ("count", "value_gbp", "aged_sum", "lact_sum", "lact_count"):
                    all_cats[cat][field_name] = (
                        all_cats[cat][field_name]
                        + farm_agg.get(farm, {}).get(cat, {}).get(field_name, 0)
                    )
            all_meta["dairy_cows"] += farm_meta.get(farm, {}).get("dairy_cows", 0)
        totals["all"] = _farm_summary_row(all_cats, all_meta)

        months.append(
            {
                "month_start": month_start.isoformat(),
                "month_label": month_start.strftime("%b-%y"),
                "close_date": close_date.isoformat(),
                "totals": totals,
                "grand_total_gbp": totals["all"]["grand_total_gbp"],
            }
        )
    return months


def _selected_month_detail(
    months: list[dict[str, Any]],
    selected_month: dt.date | None,
) -> dict[str, Any] | None:
    if not months:
        return None
    target_month = _month_start(selected_month) if selected_month else _month_start(
        dt.date.fromisoformat(months[-1]["month_start"])
    )
    for month_row in months:
        if month_row["month_start"] == target_month.isoformat():
            return {
                "month_start": month_row["month_start"],
                "month_label": month_row["month_label"],
                "close_date": month_row["close_date"],
                **_build_kpi_detail(month_row["totals"]),
            }
    last = months[-1]
    return {
        "month_start": last["month_start"],
        "month_label": last["month_label"],
        "close_date": last["close_date"],
        **_build_kpi_detail(last["totals"]),
    }


def _compute_stock_valuations_report(
    db: Session,
    *,
    selected_farms: list[str],
    anchor_ts: dt.datetime,
    fiscal_year: int,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    selected_month: dt.date | None = None,
) -> tuple[dt.date, dict[str, str], list[dict[str, Any]], dict[str, Any] | None]:
    profile_farms = list(HERD_FARM_OPTIONS)
    anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = _build_profiles(
        db,
        selected_farms=profile_farms,
        anchor_ts=anchor_ts,
    )

    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    available_end = min(fy_end, anchor_date)
    slider_min = _month_start(fy_start)
    slider_max = _month_start(available_end)
    effective_from = _month_start(month_from) if month_from is not None else slider_min
    effective_to = _month_start(month_to) if month_to is not None else slider_max
    effective_from = max(effective_from, slider_min)
    effective_to = min(effective_to, slider_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    month_starts = [
        month_start
        for month_start in _iter_month_starts(fy_start, available_end)
        if effective_from <= month_start <= effective_to
    ]

    date_bounds = {
        "min": slider_min.isoformat(),
        "max": _month_end(slider_max).isoformat(),
    }
    months = _compute_month_rows(
        month_starts=month_starts,
        anchor_date=anchor_date,
        selected_farms=selected_farms,
        profiles=profiles,
        inventory_keys=inventory_keys,
        exit_keys=exit_keys,
        entry_keys=entry_keys,
        jv_keys=jv_keys,
    )
    return anchor_date, date_bounds, months, _selected_month_detail(months, selected_month)


def rebuild_stock_valuation_snapshots(db: Session) -> dict[str, Any]:
    """Recompute and persist month-end valuations for all farms and fiscal years."""
    anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    if anchor_ts is None:
        db.execute(delete(StockValuationSnapshot))
        db.commit()
        return {"anchor_import_timestamp": None, "rows_written": 0, "fiscal_years": []}

    farms = list(HERD_FARM_OPTIONS)
    anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = _build_profiles(
        db,
        selected_farms=farms,
        anchor_ts=anchor_ts,
    )
    fiscal_years = _fiscal_year_options(db, farms)

    db.execute(delete(StockValuationSnapshot))
    rows_written = 0
    for fiscal_year in fiscal_years:
        fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
        available_end = min(fy_end, anchor_date)
        for month_start in _iter_month_starts(fy_start, available_end):
            close_date = min(_month_end(month_start), anchor_date)
            if close_date < month_start:
                continue
            keys = _on_farm_keys(
                close_date,
                anchor_date,
                inventory_keys,
                exit_keys,
                entry_keys,
                jv_keys,
                profiles,
            )
            farm_agg, farm_meta = _aggregate_animals(
                profiles, keys, close_date, anchor_date=anchor_date
            )
            for farm in farms:
                db.add(
                    _snapshot_from_farm_month(
                        anchor_ts=anchor_ts,
                        farm=farm,
                        month_start=month_start,
                        close_date=close_date,
                        farm_data=farm_agg.get(farm, _empty_category_totals()),
                        farm_meta=farm_meta.get(farm, _empty_farm_meta()),
                    )
                )
                rows_written += 1

    db.commit()
    gc.collect()
    return {
        "anchor_import_timestamp": anchor_ts.isoformat(timespec="seconds"),
        "rows_written": rows_written,
        "fiscal_years": fiscal_years,
    }


def _report_from_snapshots(
    db: Session,
    *,
    selected_farms: list[str],
    anchor_ts: dt.datetime,
    anchor_date: dt.date,
    fiscal_year: int,
    fiscal_year_options: list[int],
    month_from: dt.date | None,
    month_to: dt.date | None,
    selected_month: dt.date | None,
) -> dict[str, Any]:
    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    available_end = min(fy_end, anchor_date)
    slider_min = _month_start(fy_start)
    slider_max = _month_start(available_end)
    effective_from = _month_start(month_from) if month_from is not None else slider_min
    effective_to = _month_start(month_to) if month_to is not None else slider_max
    effective_from = max(effective_from, slider_min)
    effective_to = min(effective_to, slider_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    snapshots = db.scalars(
        select(StockValuationSnapshot)
        .where(StockValuationSnapshot.anchor_import_timestamp == anchor_ts)
        .where(StockValuationSnapshot.farm.in_(selected_farms))
        .where(StockValuationSnapshot.month_start >= effective_from)
        .where(StockValuationSnapshot.month_start <= effective_to)
        .order_by(StockValuationSnapshot.month_start.asc())
    ).all()

    by_month: dict[str, list[StockValuationSnapshot]] = {}
    for snapshot in snapshots:
        key = snapshot.month_start.isoformat()
        by_month.setdefault(key, []).append(snapshot)

    months: list[dict[str, Any]] = []
    for month_key in sorted(by_month):
        month_snaps = by_month[month_key]
        month_start = month_snaps[0].month_start
        close_date = month_snaps[0].close_date
        totals: dict[str, Any] = {}
        merged_cats = _empty_category_totals()
        merged_meta = _empty_farm_meta()
        for snapshot in month_snaps:
            farm_data, farm_meta = _farm_data_from_snapshot(snapshot)
            totals[snapshot.farm] = _farm_summary_row(farm_data, farm_meta)
            for cat in CATEGORIES:
                for field_name in ("count", "value_gbp", "aged_sum", "lact_sum", "lact_count"):
                    merged_cats[cat][field_name] = (
                        merged_cats[cat][field_name] + farm_data[cat][field_name]
                    )
            merged_meta["dairy_cows"] += farm_meta["dairy_cows"]
        totals["all"] = _farm_summary_row(merged_cats, merged_meta)
        months.append(
            {
                "month_start": month_start.isoformat(),
                "month_label": month_start.strftime("%b-%y"),
                "close_date": close_date.isoformat(),
                "totals": totals,
                "grand_total_gbp": totals["all"]["grand_total_gbp"],
            }
        )

    return {
        "anchor_date": anchor_date.isoformat(),
        "fiscal_year": fiscal_year,
        "fiscal_year_options": fiscal_year_options,
        "date_bounds": {
            "min": slider_min.isoformat(),
            "max": _month_end(slider_max).isoformat(),
        },
        "months": months,
        "selected_month": _selected_month_detail(months, selected_month),
        "methodology": METHODOLOGY_SUMMARY,
        "from_snapshot": True,
    }


def build_stock_valuations_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    fiscal_year: int | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    selected_month: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    empty = {
        "anchor_date": None,
        "fiscal_year": fiscal_year,
        "fiscal_year_options": [],
        "date_bounds": None,
        "months": [],
        "selected_month": None,
        "methodology": METHODOLOGY_SUMMARY,
    }
    if not selected_farms:
        return empty

    anchor_ts = db.scalar(
        select(func.max(HerdInventory.import_timestamp)).where(
            HerdInventory.farm.in_(selected_farms)
        )
    )
    if anchor_ts is None:
        empty["fiscal_year_options"] = _fiscal_year_options(db, selected_farms)
        return empty

    fiscal_year_options = _fiscal_year_options(db, selected_farms)
    if fiscal_year is None and fiscal_year_options:
        fiscal_year = fiscal_year_options[0]

    anchor_date = anchor_ts.date()
    if fiscal_year is None:
        return {
            **empty,
            "anchor_date": anchor_date.isoformat(),
            "fiscal_year_options": fiscal_year_options,
        }

    if _snapshot_has_data(db, anchor_ts):
        result = _report_from_snapshots(
            db,
            selected_farms=selected_farms,
            anchor_ts=anchor_ts,
            anchor_date=anchor_date,
            fiscal_year=fiscal_year,
            fiscal_year_options=fiscal_year_options,
            month_from=month_from,
            month_to=month_to,
            selected_month=selected_month,
        )
    else:
        _, date_bounds, months, selected_detail = _compute_stock_valuations_report(
            db,
            selected_farms=selected_farms,
            anchor_ts=anchor_ts,
            fiscal_year=fiscal_year,
            month_from=month_from,
            month_to=month_to,
            selected_month=selected_month,
        )
        result = {
            "anchor_date": anchor_date.isoformat(),
            "fiscal_year": fiscal_year,
            "fiscal_year_options": fiscal_year_options,
            "date_bounds": date_bounds,
            "months": months,
            "selected_month": selected_detail,
            "methodology": METHODOLOGY_SUMMARY,
            "from_snapshot": False,
        }

    return result
