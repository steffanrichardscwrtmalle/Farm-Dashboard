"""Cows-in-milk counts from inventory + events."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CowEvent, HerdInventory
from app.services.cows_in_milk import cows_in_milk_for_dates, current_cows_in_milk


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_current_cows_in_milk_excludes_dry_and_low_dim() -> None:
    db = _session()
    db.add_all(
        [
            HerdInventory(
                farm="GAD", cow_id="1", lact=2, rpro="PREG", dim=100, fdat=dt.date(2026, 1, 1)
            ),
            HerdInventory(
                farm="GAD", cow_id="2", lact=1, rpro="DRY", dim=300, fdat=dt.date(2025, 6, 1)
            ),
            HerdInventory(
                farm="GAD", cow_id="3", lact=0, rpro="", dim=None, fdat=None
            ),
            HerdInventory(
                farm="GAD", cow_id="4", lact=1, rpro="FRESH", dim=3, fdat=dt.date(2026, 8, 6)
            ),
            HerdInventory(
                farm="CM", cow_id="9", lact=3, rpro="FRESH", dim=20, fdat=dt.date(2026, 7, 1)
            ),
        ]
    )
    db.commit()
    assert current_cows_in_milk(db, "GAD") == 1
    assert current_cows_in_milk(db, "CM") == 1


def test_historical_count_uses_fresh_and_dry_events() -> None:
    db = _session()
    db.add_all(
        [
            CowEvent(
                farm="CM",
                cow_id="100",
                event="FRESH",
                event_date=dt.date(2026, 1, 1),
                lact=1,
            ),
            CowEvent(
                farm="CM",
                cow_id="100",
                event="DRY",
                event_date=dt.date(2026, 6, 1),
                lact=1,
            ),
            CowEvent(
                farm="CM",
                cow_id="200",
                event="FRESH",
                event_date=dt.date(2026, 3, 1),
                lact=1,
            ),
            CowEvent(
                farm="CM",
                cow_id="200",
                event="SOLD",
                event_date=dt.date(2026, 4, 15),
                lact=1,
            ),
        ]
    )
    db.commit()
    counts = cows_in_milk_for_dates(
        db,
        ["CM"],
        [
            dt.date(2025, 12, 31),
            dt.date(2026, 1, 5),  # DIM 4 — excluded
            dt.date(2026, 1, 6),  # DIM 5 — counted
            dt.date(2026, 2, 1),
            dt.date(2026, 3, 5),  # 200 still in fresh window
            dt.date(2026, 3, 10),
            dt.date(2026, 4, 15),
            dt.date(2026, 5, 1),
            dt.date(2026, 6, 1),
        ],
    )
    assert counts[("CM", dt.date(2025, 12, 31))] == 0
    assert counts[("CM", dt.date(2026, 1, 5))] == 0
    assert counts[("CM", dt.date(2026, 1, 6))] == 1
    assert counts[("CM", dt.date(2026, 2, 1))] == 1
    assert counts[("CM", dt.date(2026, 3, 5))] == 1
    assert counts[("CM", dt.date(2026, 3, 10))] == 2
    assert counts[("CM", dt.date(2026, 4, 15))] == 1  # 200 sold that day
    assert counts[("CM", dt.date(2026, 5, 1))] == 1
    assert counts[("CM", dt.date(2026, 6, 1))] == 0  # 100 dry that day


def test_inventory_fdat_covers_missing_fresh_event() -> None:
    db = _session()
    db.add(
        HerdInventory(
            farm="GAD",
            cow_id="77",
            lact=1,
            rpro="FRESH",
            dim=10,
            fdat=dt.date(2026, 7, 20),
        )
    )
    db.commit()
    today = dt.date.today()
    counts = cows_in_milk_for_dates(
        db, ["GAD"], [dt.date(2026, 7, 24), dt.date(2026, 7, 25), today]
    )
    assert counts[("GAD", dt.date(2026, 7, 24))] == 0  # DIM 4
    assert counts[("GAD", dt.date(2026, 7, 25))] == 1  # DIM 5
    assert counts[("GAD", today)] == 1
