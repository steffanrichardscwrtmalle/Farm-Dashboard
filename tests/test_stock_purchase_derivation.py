"""Tests for purchase stock-group classification and derivation."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    Base,
    CowEvent,
    HerdInventory,
    StockPurchaseAnimal,
)
from app.services.stock_purchase_derivation import (
    classify_purchase_stock_group,
    rebuild_stock_purchases,
)


def test_purchased_heifer_fresh_after_arrival_is_youngstock() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2024, 12, 31),
            min_lact=1,
        )
        == STOCK_GROUP_YOUNGSTOCK
    )


def test_purchased_milking_cow_stays_cows() -> None:
    assert (
        classify_purchase_stock_group(
            3,
            10,
            "F",
            edat=dt.date(2020, 12, 23),
            fdat=dt.date(2021, 12, 30),
            min_lact=3,
        )
        == STOCK_GROUP_COWS
    )


def test_purchased_first_lact_cow_with_fdat_before_arrival_stays_cows() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 6, 1),
            fdat=dt.date(2024, 2, 1),
            min_lact=1,
        )
        == STOCK_GROUP_COWS
    )


def test_purchased_heifer_with_lact_zero_history_is_youngstock() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2024, 12, 31),
            min_lact=0,
        )
        == STOCK_GROUP_YOUNGSTOCK
    )


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            CowEvent.__table__,
            HerdInventory.__table__,
            StockPurchaseAnimal.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_rebuild_keeps_event_history_and_adds_inventory_only_purchases() -> None:
    session = _session()
    arrival = dt.date(2025, 6, 1)
    session.add(
        CowEvent(
            farm="CM",
            etag="UK111111111111",
            edat=arrival,
            bdat=dt.date(2022, 1, 1),
            event_date=arrival,
            lact=3,
            cbrd=10,
            gndr="F",
            fdat=dt.date(2023, 1, 1),
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK111111111111",
            edat=dt.date(2025, 7, 1),
            bdat=dt.date(2022, 1, 1),
            lact=4,
            cbrd=10,
            gender="Female",
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK222222222222",
            edat=dt.date(2025, 8, 1),
            bdat=dt.date(2024, 8, 1),
            lact=0,
            cbrd=10,
            gender="Female",
            fdat=None,
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK333333333333",
            edat=dt.date(2024, 1, 1),
            bdat=dt.date(2024, 1, 1),
            lact=0,
            cbrd=10,
            gender="Female",
        )
    )
    session.commit()

    stats = rebuild_stock_purchases(session)
    session.commit()

    rows = {
        row.etag: row
        for row in session.scalars(select(StockPurchaseAnimal)).all()
    }
    assert stats["from_events"] == 1
    assert stats["from_inventory"] == 1
    assert stats["changed"] is True
    assert set(rows) == {"UK111111111111", "UK222222222222"}
    assert rows["UK111111111111"].edat == arrival
    assert rows["UK111111111111"].lact == 3
    assert rows["UK111111111111"].stock_group == STOCK_GROUP_COWS
    assert rows["UK222222222222"].stock_group == STOCK_GROUP_YOUNGSTOCK
    assert rows["UK222222222222"].gndr == "F"

    again = rebuild_stock_purchases(session)
    assert again["changed"] is False
    assert session.scalar(select(func.count()).select_from(StockPurchaseAnimal)) == 2
    session.close()


def test_rebuild_excludes_gad_inventory_purchases() -> None:
    session = _session()
    session.add(
        HerdInventory(
            farm="GAD",
            etag="UK752261400001",
            edat=dt.date(2025, 3, 31),
            bdat=dt.date(2023, 1, 1),
            lact=2,
            cbrd=10,
            gender="Female",
        )
    )
    session.add(
        HerdInventory(
            farm="GAD",
            etag="UK240837106626",
            edat=dt.date(2025, 8, 1),
            bdat=dt.date(2020, 1, 1),
            lact=4,
            cbrd=10,
            gender="Female",
        )
    )
    session.add(
        HerdInventory(
            farm="GAD",
            etag="UK999999999999",
            edat=dt.date(2025, 8, 1),
            bdat=dt.date(2024, 1, 1),
            lact=1,
            cbrd=110,
            gender="Male",
        )
    )
    session.commit()

    stats = rebuild_stock_purchases(session)
    rows = list(session.scalars(select(StockPurchaseAnimal)).all())
    assert stats["excluded_count"] == 2
    assert stats["from_inventory"] == 1
    assert len(rows) == 1
    assert rows[0].etag == "UK999999999999"
    assert rows[0].stock_group == STOCK_GROUP_BEEF
    assert rows[0].gndr == "M"
    session.close()


def test_beef_purchase_ignores_fdat() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            102,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2025, 1, 1),
        )
        == STOCK_GROUP_BEEF
    )
