from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent
from app.services.stock_purchases import normalize_stock_group


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_normalize_stock_group_accepts_beef() -> None:
    assert normalize_stock_group("beef") == "beef"


def test_normalize_stock_group_defaults_unknown_to_cows() -> None:
    assert normalize_stock_group("invalid") == "cows"


def test_beef_fresh_not_counted_as_youngstock_calving(db: Session) -> None:
    """Beef-breed FRESH lact=1 events must not reduce youngstock closing."""
    from app.services.stock_accruals import _fetch_event_count_by_month

    db.add(
        CowEvent(
            farm="CM",
            cow_id="836",
            etag="UK740651424836",
            event="FRESH",
            event_date=dt.date(2024, 8, 12),
            lact=1,
            cbrd=254,
            gndr="F",
            bdat=dt.date(2022, 4, 2),
        )
    )
    db.add(
        CowEvent(
            farm="CM",
            cow_id="100",
            etag="UK100",
            event="FRESH",
            event_date=dt.date(2024, 8, 15),
            lact=1,
            cbrd=1,
            gndr="F",
            bdat=dt.date(2022, 4, 2),
        )
    )
    db.commit()

    counts = _fetch_event_count_by_month(
        db,
        farm="CM",
        stock_group="youngstock",
        event_type="FRESH",
        month_from=dt.date(2024, 8, 1),
        month_to=dt.date(2024, 8, 31),
        lact_filter="fresh_heifers",
    )
    assert counts.get((2024, 8)) == 1


def test_rebuild_accrual_snapshots_served_from_table(db: Session) -> None:
    from app.models import HerdInventory, StockOpeningBaseline
    from app.services.stock_accruals import (
        build_stock_accruals_report,
        rebuild_stock_accrual_snapshots,
    )

    anchor_ts = dt.datetime(2026, 6, 30, 12, 0, 0)
    db.add(
        StockOpeningBaseline(
            farm="CM",
            stock_group="cows",
            month_start=dt.date(2024, 4, 1),
            opening_count=100,
        )
    )
    db.add(
        HerdInventory(
            farm="CM",
            cow_id="1",
            etag="UK1",
            bdat=dt.date(2020, 1, 1),
            lact=2,
            import_timestamp=anchor_ts,
        )
    )
    db.add(
        CowEvent(
            farm="CM",
            cow_id="1",
            etag="UK1",
            event="SOLD",
            event_date=dt.date(2026, 3, 10),
            lact=2,
            remark="",
            bdat=dt.date(2020, 1, 1),
        )
    )
    db.commit()

    live = build_stock_accruals_report(
        db,
        farms=["CM"],
        stock_group="cows",
        month_from=dt.date(2026, 3, 1),
        month_to=dt.date(2026, 3, 31),
    )
    assert live.get("from_snapshot") is False
    assert live["rows"]

    stats = rebuild_stock_accrual_snapshots(db)
    assert stats["rows_written"] > 0

    cached = build_stock_accruals_report(
        db,
        farms=["CM"],
        stock_group="cows",
        month_from=dt.date(2026, 3, 1),
        month_to=dt.date(2026, 3, 31),
    )
    assert cached.get("from_snapshot") is True
    assert cached["rows"] == live["rows"]
