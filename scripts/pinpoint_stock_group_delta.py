"""Find exact animals causing stock-group count delta vs Stock Accruals at month close."""
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

_STOCK_GROUPS = {
    "cows": STOCK_GROUP_COWS,
    "youngstock": STOCK_GROUP_YOUNGSTOCK,
    "beef": STOCK_GROUP_BEEF,
}


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
                if sg == pool:
                    pool = None
                on_farm = False
            elif snap.event == "FRESH" and snap.lact == 1 and on_farm:
                if pool == STOCK_GROUP_YOUNGSTOCK:
                    pool = STOCK_GROUP_COWS

    if not on_farm or pool is None:
        return None
    return pool


def analyse_month(*, farm: str, year: int, month: int, stock_group: str) -> None:
    target_sg = _STOCK_GROUPS[stock_group]
    init_db()
    db = SessionLocal()
    try:
        anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
        anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = (
            _build_profiles(db, selected_farms=["CM", "GAD"], anchor_ts=anchor_ts)
        )
        month_start = dt.date(year, month, 1)
        close_date = min(_month_end(month_start), anchor_date)
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

        acc_closing = _accruals_closing_for_month(
            db,
            farm=farm,
            stock_group=target_sg,
            month_start=month_start,
            fiscal_year=fiscal_year,
        )

        val_tags: set[str] = set()
        pool_tags: set[str] = set()
        only_val: list[dict] = []
        only_pool: list[dict] = []
        no_snap_val: list[str] = []

        for key in keys:
            profile = profiles.get(key)
            if profile is None or profile.farm != farm:
                continue
            if not _animal_on_farm_at_close(profile, close_date):
                continue

            state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
            val_g = state["stock_group"] if state else None
            pool = _accruals_pool_at(profile, close_date, month_start=month_start)
            since, stint = _stint_at_close(profile, close_date)
            snap = profile.best_event_on_or_before(close_date, since=since)

            if val_g == target_sg:
                val_tags.add(profile.etag)
                if snap is None:
                    no_snap_val.append(profile.etag)
            if pool == target_sg:
                pool_tags.add(profile.etag)

            detail = {
                "etag": profile.etag,
                "val": val_g,
                "pool": pool,
                "since": since,
                "stint": stint.stock_group if stint else None,
                "snap": snap is not None,
            }

            if val_g == target_sg and pool != target_sg:
                only_val.append(detail)
            if pool == target_sg and val_g != target_sg:
                only_pool.append(detail)

        print(f"\n=== {farm} {month_start} {stock_group} close {close_date} FY{fiscal_year} ===")
        print(f"Accruals closing: {acc_closing}")
        print(f"Valuations count: {len(val_tags)}  pool={len(pool_tags)}")
        print(f"Delta (val-acc): {len(val_tags) - acc_closing if acc_closing is not None else '?'}")

        print(f"\nVal={stock_group}, pool!={stock_group} ({len(only_val)}):")
        for row in only_val[:15]:
            print(f"  {row}")

        print(f"\nPool={stock_group}, val!={stock_group} ({len(only_pool)}):")
        for row in only_pool[:15]:
            print(f"  {row}")

        if stock_group == "youngstock" and no_snap_val:
            print(f"\nNo-snap youngstock ({len(no_snap_val)}): {no_snap_val}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--farm", default="CM")
    parser.add_argument("--month", default="2024-04", help="YYYY-MM")
    parser.add_argument(
        "--stock-group",
        default="youngstock",
        choices=sorted(_STOCK_GROUPS),
    )
    args = parser.parse_args()
    year, month = map(int, args.month.split("-"))
    analyse_month(farm=args.farm, year=year, month=month, stock_group=args.stock_group)


if __name__ == "__main__":
    main()
