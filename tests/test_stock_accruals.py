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
