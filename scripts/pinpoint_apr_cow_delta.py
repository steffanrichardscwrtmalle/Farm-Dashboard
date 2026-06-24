"""Find exact animals causing CM cow count delta vs Stock Accruals at a month close."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.models import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    HerdInventory,
)
from app.services.stock_group import (
    stock_group_from_birth,
    stock_group_from_event_fields,
)
from app.services.stock_valuations import (
    AnimalProfile,
    EventSnapshot,
    _accruals_closing_for_month,
    _animal_on_farm_at_close,
    _build_profiles,
    _month_end,
    _on_farm_keys,
    _resolve_state_at,
    _stint_at_close,
)

_EXIT = ("SOLD", "DIED")


def _snap_group_at(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    since: dt.date | None,
) -> str:
    snap = profile.best_event_on_or_before(close_date, since=since)
    lact = 0
    _, stint = _stint_at_close(profile, close_date)
    if stint is not None:
        lact = max(lact, int(stint.lact or 0))
    if snap is not None:
        lact = max(lact, int(snap.lact or 0))
    for ev in profile.events:
        if ev.event_date > close_date:
            continue
        if since is not None and ev.event_date < since:
            continue
        if ev.event == "FRESH" and ev.lact == 1:
            lact = max(lact, 1)
    cbrd = snap.cbrd if snap else (stint.cbrd if stint else None)
    gndr = snap.gndr if snap else (stint.gndr if stint else None)
    return stock_group_from_event_fields(lact, cbrd, gndr)


def _accruals_pool_at(
    profile: AnimalProfile,
    close_date: dt.date,
    *,
    month_start: dt.date,
) -> str | None:
    """Forward accruals pool through month_start..close_date (inclusive)."""
    since, stint = _stint_at_close(profile, close_date)
    if since is None and stint is None and not profile.bdat:
        return None

    pool: str | None = None
    if since is not None and since < month_start:
        pool = _snap_group_at(profile, month_start - dt.timedelta(days=1), since=since)
        if pool == STOCK_GROUP_BEEF:
            pool = STOCK_GROUP_YOUNGSTOCK
    elif stint is not None and stint.edat < month_start:
        pool = stint.stock_group
    elif since == profile.bdat and profile.bdat and profile.bdat < month_start:
        pool = stock_group_from_birth(
            profile.birth_category, profile.birth_cbrd, profile.birth_gndr
        )

    timeline: list[tuple[dt.date, int, str, object]] = []
    if profile.bdat:
        timeline.append((profile.bdat, 0, "birth", None))
    for purchase in profile.purchases:
        timeline.append((purchase.edat, 1, "purchase", purchase))
    for snap in profile.events:
        timeline.append((snap.event_date, 2, "event", snap))
    timeline.sort(key=lambda row: (row[0], row[1], getattr(row[3], "seq", 0)))

    on_farm = pool is not None
    for event_date, _, kind, payload in timeline:
        if event_date < month_start:
            continue
        if event_date > close_date:
            break
        if kind == "birth":
            on_farm = True
            pool = stock_group_from_birth(
                profile.birth_category, profile.birth_cbrd, profile.birth_gndr
            )
        elif kind == "purchase":
            rec = payload
            on_farm = True
            pool = rec.stock_group  # type: ignore[union-attr]
        elif kind == "event":
            snap: EventSnapshot = payload  # type: ignore[assignment]
            if snap.event in _EXIT:
                sg = stock_group_from_event_fields(snap.lact, snap.cbrd, snap.gndr)
                if sg == STOCK_GROUP_COWS and pool == STOCK_GROUP_COWS:
                    pool = None
                elif sg == STOCK_GROUP_YOUNGSTOCK and pool == STOCK_GROUP_YOUNGSTOCK:
                    pool = None
                elif sg == STOCK_GROUP_BEEF and pool == STOCK_GROUP_BEEF:
                    pool = None
                on_farm = False
            elif snap.event == "FRESH" and snap.lact == 1 and on_farm:
                if pool == STOCK_GROUP_YOUNGSTOCK:
                    pool = STOCK_GROUP_COWS

    if not on_farm or pool is None:
        return None
    return pool


def _val_group_at(
    profile: AnimalProfile,
    close_date: dt.date,
    anchor_date: dt.date,
) -> str | None:
    state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
    return state["stock_group"] if state else None


def analyse_month(*, farm: str, year: int, month: int) -> None:
    init_db()
    db = SessionLocal()
    try:
        anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
        anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = (
            _build_profiles(
                db, selected_farms=["CM", "GAD"], anchor_ts=anchor_ts
            )
        )
        month_start = dt.date(year, month, 1)
        close_date = min(_month_end(month_start), anchor_date)
        fiscal_year = year if month >= 4 else year - 1
        if month >= 4:
            fiscal_year = year + 1 if month >= 4 else year
        # UK fiscal: Apr 2024 -> FY 2025
        fiscal_year = year + 1 if month >= 4 else year

        keys = _on_farm_keys(
            close_date,
            anchor_date,
            inventory_keys,
            exit_keys,
            entry_keys,
            jv_keys,
            profiles,
        )

        acc_cows = _accruals_closing_for_month(
            db,
            farm=farm,
            stock_group=STOCK_GROUP_COWS,
            month_start=month_start,
            fiscal_year=fiscal_year,
        )
        acc_ys = _accruals_closing_for_month(
            db,
            farm=farm,
            stock_group=STOCK_GROUP_YOUNGSTOCK,
            month_start=month_start,
            fiscal_year=fiscal_year,
        )

        val_cows: set[str] = set()
        val_ys: set[str] = set()
        acc_pool_cows: set[str] = set()
        acc_pool_ys: set[str] = set()
        snap_cows: set[str] = set()

        only_val_cow: list[dict] = []
        only_acc_pool_cow: list[dict] = []
        val_cow_acc_ys: list[dict] = []

        for key in keys:
            profile = profiles.get(key)
            if profile is None or profile.farm != farm:
                continue
            if not _animal_on_farm_at_close(profile, close_date):
                continue

            val_g = _val_group_at(profile, close_date, anchor_date)
            pool = _accruals_pool_at(
                profile, close_date, month_start=month_start
            )
            since, stint = _stint_at_close(profile, close_date)
            snap_g = _snap_group_at(profile, close_date, since=since)

            if val_g == STOCK_GROUP_COWS:
                val_cows.add(profile.etag)
            if val_g == STOCK_GROUP_YOUNGSTOCK:
                val_ys.add(profile.etag)
            if pool == STOCK_GROUP_COWS:
                acc_pool_cows.add(profile.etag)
            if pool == STOCK_GROUP_YOUNGSTOCK:
                acc_pool_ys.add(profile.etag)
            if snap_g == STOCK_GROUP_COWS:
                snap_cows.add(profile.etag)

            detail = {
                "etag": profile.etag,
                "val": val_g,
                "pool": pool,
                "snap": snap_g,
                "stint": stint.stock_group if stint else None,
                "since": since,
            }

            if val_g == STOCK_GROUP_COWS and pool != STOCK_GROUP_COWS:
                val_cow_acc_ys.append(detail)
            if val_g == STOCK_GROUP_COWS and profile.etag not in acc_pool_cows:
                only_val_cow.append(detail)
            if pool == STOCK_GROUP_COWS and val_g != STOCK_GROUP_COWS:
                only_acc_pool_cow.append(detail)

        print(f"\n=== {farm} {month_start} close {close_date} FY{fiscal_year} ===")
        print(
            f"Accruals closing: cows={acc_cows} youngstock={acc_ys} "
            f"c+ys={acc_cows + acc_ys if acc_cows and acc_ys else '?'}"
        )
        print(
            f"Valuations:       cows={len(val_cows)} youngstock={len(val_ys)} "
            f"snap_cows={len(snap_cows)} pool_cows={len(acc_pool_cows)}"
        )
        print(
            f"Delta cows (val-acc): {len(val_cows) - acc_cows if acc_cows else '?'}"
        )
        print(
            f"Delta ys   (val-acc): {len(val_ys) - acc_ys if acc_ys else '?'}"
        )

        print(f"\nVal=cow, forward pool!=cow ({len(val_cow_acc_ys)}):")
        for row in val_cow_acc_ys:
            print(f"  {row}")

        print(f"\nVal=cow, not in forward pool cows ({len(only_val_cow)}):")
        for row in only_val_cow[:20]:
            print(f"  {row}")

        print(f"\nPool=cow, val!=cow ({len(only_acc_pool_cow)}):")
        for row in only_acc_pool_cow[:10]:
            print(f"  {row}")

        # Bisect: animals in snap cows but accruals closing 2 short
        if acc_cows and len(snap_cows) - acc_cows <= 5:
            snap_not_pool = sorted(snap_cows - acc_pool_cows)
            pool_not_snap = sorted(acc_pool_cows - snap_cows)
            print(f"\nSnap=cow but pool!=cow ({len(snap_not_pool)}): {snap_not_pool[:10]}")
            print(f"Pool=cow but snap!=cow ({len(pool_not_snap)}): {pool_not_snap[:10]}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--farm", default="CM")
    parser.add_argument("--month", default="2024-04", help="YYYY-MM")
    args = parser.parse_args()
    year, month = map(int, args.month.split("-"))
    analyse_month(farm=args.farm, year=year, month=month)


if __name__ == "__main__":
    main()
