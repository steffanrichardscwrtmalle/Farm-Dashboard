"""Find per-animal stock-group mismatches between valuations and accruals-style forward rules."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.models import STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK, HerdInventory
from app.services.stock_group import (
    stock_group_from_birth,
    stock_group_from_event_fields,
    valuation_category_from_stock_group,
)
from app.services.stock_valuations import (
    AnimalProfile,
    EventSnapshot,
    PurchaseRecord,
    _build_profiles,
    _month_end,
    _on_farm_keys,
    _resolve_state_at,
    _stint_at_close,
    _effective_lact_at_close,
)

_EXIT_EVENTS = ("SOLD", "DIED")


def forward_stock_group_at(profile: AnimalProfile, close_date: dt.date) -> str | None:
    """Accruals-style bucket: birth/purchase entry, FRESH lact>=1 YS->cows, exit clears."""
    on_farm = False
    bucket: str | None = None

    timeline: list[tuple[dt.date, int, str, object]] = []
    if profile.bdat is not None:
        timeline.append(
            (profile.bdat, 0, "birth", None),
        )
    for purchase in profile.purchases:
        timeline.append((purchase.edat, 1, "purchase", purchase))
    for snap in profile.events:
        timeline.append((snap.event_date, 2, "event", snap))
    timeline.sort(key=lambda row: (row[0], row[1], getattr(row[3], "seq", 0)))

    for event_date, _, kind, payload in timeline:
        if event_date > close_date:
            break
        if kind == "birth":
            on_farm = True
            bucket = stock_group_from_birth(
                profile.birth_category, profile.birth_cbrd, profile.birth_gndr
            )
        elif kind == "purchase":
            on_farm = True
            bucket = payload.stock_group  # type: ignore[union-attr]
        elif kind == "event":
            snap: EventSnapshot = payload  # type: ignore[assignment]
            if snap.event in _EXIT_EVENTS:
                on_farm = False
                bucket = None
            elif snap.event == "FRESH" and (snap.lact or 0) >= 1 and on_farm:
                if bucket == STOCK_GROUP_YOUNGSTOCK:
                    bucket = STOCK_GROUP_COWS

    return bucket if on_farm else None


def accruals_stock_group_at(
    profile: AnimalProfile,
    close_date: dt.date,
) -> str | None:
    """Classify like accruals: pool from stint + FRESH lact==1; snap lact at close."""
    since, stint_purchase = _stint_at_close(profile, close_date)
    if since is None and stint_purchase is None:
        return None

    pool: str | None = None
    if stint_purchase is not None and stint_purchase.edat <= close_date:
        pool = stint_purchase.stock_group
    elif profile.bdat is not None and since == profile.bdat:
        pool = stock_group_from_birth(
            profile.birth_category, profile.birth_cbrd, profile.birth_gndr
        )

    for snap in profile.events:
        if snap.event_date > close_date:
            continue
        if since is not None and snap.event_date < since:
            continue
        if snap.event == "FRESH" and snap.lact == 1 and pool == STOCK_GROUP_YOUNGSTOCK:
            pool = STOCK_GROUP_COWS

    best = profile.best_event_on_or_before(close_date, since=since)
    if best is not None:
        lact = int(best.lact or 0)
        if stint_purchase is not None:
            lact = max(lact, int(stint_purchase.lact or 0))
        snap_group = stock_group_from_event_fields(lact, best.cbrd, best.gndr)
        if pool == STOCK_GROUP_COWS:
            return STOCK_GROUP_COWS
        if pool == STOCK_GROUP_YOUNGSTOCK and snap_group == STOCK_GROUP_COWS:
            return STOCK_GROUP_YOUNGSTOCK
        return snap_group
    return pool


def diagnose_month(db, farm: str, close_date: dt.date) -> None:
    anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = _build_profiles(
        db, selected_farms=["CM", "GAD"], anchor_ts=anchor_ts
    )
    keys = _on_farm_keys(
        close_date, anchor_date, inventory_keys, exit_keys, entry_keys, jv_keys
    )

    val_cows: set[tuple[str, str]] = set()
    acc_cows: set[tuple[str, str]] = set()
    mismatches: list[dict] = []

    for key in sorted(keys):
        profile = profiles.get(key)
        if profile is None or profile.farm != farm:
            continue
        state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
        if state is None:
            continue
        val_sg = state["stock_group"]
        acc_sg = accruals_stock_group_at(profile, close_date)
        if val_sg == STOCK_GROUP_COWS:
            val_cows.add(key)
        if acc_sg == STOCK_GROUP_COWS:
            acc_cows.add(key)
        if val_sg != acc_sg:
            since, stint = _stint_at_close(profile, close_date)
            fresh_heifer = any(
                s.event == "FRESH" and s.lact == 1
                and (since is None or s.event_date >= since)
                and s.event_date <= close_date
                for s in profile.events
            )
            fresh_lact_gt1 = any(
                s.event == "FRESH" and (s.lact or 0) > 1
                and (since is None or s.event_date >= since)
                and s.event_date <= close_date
                for s in profile.events
            )
            mismatches.append(
                {
                    "etag": profile.etag,
                    "val": val_sg,
                    "acc": acc_sg,
                    "lact": state["lact"],
                    "snap_lact": profile.best_event_on_or_before(
                        close_date, since=since
                    ),
                    "fresh_heifer": fresh_heifer,
                    "fresh_gt1": fresh_lact_gt1,
                    "stint": stint.stock_group if stint else None,
                }
            )

    only_val = val_cows - acc_cows
    only_acc = acc_cows - val_cows
    print(f"\n=== {farm} close {close_date} ===")
    print(f"Val cows: {len(val_cows)}  Acc-pool cows: {len(acc_cows)}  delta: {len(val_cows) - len(acc_cows):+d}")
    print(f"Only in val cows ({len(only_val)}):")
    for key in sorted(only_val):
        p = profiles[key]
        s = _resolve_state_at(p, close_date, anchor_date=anchor_date)
        since, stint = _stint_at_close(p, close_date)
        snap = p.best_event_on_or_before(close_date, since=since)
        print(
            f"  {p.etag} val_lact={s['lact']} snap_lact={snap.lact if snap else None} "
            f"acc={accruals_stock_group_at(p, close_date)} "
            f"stint={stint.stock_group if stint else None}"
        )
    print(f"Only in acc cows ({len(only_acc)}):")
    for key in sorted(only_acc):
        p = profiles[key]
        s = _resolve_state_at(p, close_date, anchor_date=anchor_date)
        print(
            f"  {p.etag} val={s['stock_group'] if s else None} "
            f"acc={accruals_stock_group_at(p, close_date)}"
        )
    cow_mis = [m for m in mismatches if m["val"] == STOCK_GROUP_COWS or m["acc"] == STOCK_GROUP_COWS]
    if cow_mis:
        print(f"Cow-relevant bucket mismatches ({len(cow_mis)}):")
        for m in cow_mis:
            print(f"  {m}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--farm", default="CM")
    parser.add_argument("--month", required=True, help="YYYY-MM")
    args = parser.parse_args()

    year, month = map(int, args.month.split("-"))
    close_date = _month_end(dt.date(year, month, 1))

    init_db()
    db = SessionLocal()
    try:
        diagnose_month(db, args.farm, close_date)
    finally:
        db.close()


if __name__ == "__main__":
    main()
