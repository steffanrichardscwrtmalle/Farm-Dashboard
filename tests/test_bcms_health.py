"""BCMS home-widget health status tests."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CowEvent, CtsOnHolding, HerdInventory, StockPurchaseAnimal
from app.services.bcms_health import get_bcms_health


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_health_green_when_perfect_match() -> None:
    session = _session()
    today = dt.date.today()
    session.add(
        CtsOnHolding(farm="CM", etag="UK111111111111", sex="F", breed="HF")
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK111111111111",
            cow_id="1",
            gender="F",
            bdat=today - dt.timedelta(days=400),
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "green"
    assert health["label"] == "Healthy"
    assert health["mismatch_count"] == 0


def test_health_green_when_only_yesterday_birth_and_sale() -> None:
    session = _session()
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)

    # Sale yesterday: still on CTS, left inventory
    session.add(
        CtsOnHolding(farm="CM", etag="UK222222222222", sex="F", breed="HF")
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK222222222222",
            event="SOLD",
            event_date=yesterday,
            cow_id="2",
        )
    )
    # Birth yesterday: in inventory, not on CTS
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK333333333333",
            cow_id="3",
            gender="F",
            bdat=yesterday,
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "green"
    assert health["max_days"] == 1
    assert health["mismatch_count"] == 2


def test_health_yellow_when_oldest_discrepancy_is_two_days() -> None:
    session = _session()
    today = dt.date.today()
    two_days = today - dt.timedelta(days=2)
    session.add(
        CtsOnHolding(farm="CM", etag="UK444444444444", sex="F")
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK444444444444",
            event="DIED",
            event_date=two_days,
            cow_id="4",
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "yellow"
    assert health["label"] == "Attention"
    assert health["max_days"] == 2


def test_health_red_when_discrepancy_three_plus_days() -> None:
    session = _session()
    today = dt.date.today()
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK555555555555",
            cow_id="5",
            gender="F",
            bdat=today - dt.timedelta(days=3),
        )
    )
    # Empty CTS snapshot still counts inventory-only as mismatch.
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "red"
    assert health["label"] == "Unhealthy"
    assert health["max_days"] == 3


def test_health_red_for_unexplained_cts_only() -> None:
    session = _session()
    today = dt.date.today()
    session.add(
        CtsOnHolding(farm="CM", etag="UK666666666666", sex="F")
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "red"
    assert health["mismatch_count"] == 1


def test_health_uses_purchase_date_for_move_on() -> None:
    session = _session()
    today = dt.date.today()
    # Older animal purchased yesterday — should be green, not red by DOB.
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK777777777777",
            cow_id="7",
            gender="F",
            bdat=today - dt.timedelta(days=500),
        )
    )
    session.add(
        StockPurchaseAnimal(
            farm="CM",
            etag="UK777777777777",
            edat=today - dt.timedelta(days=1),
            bdat=today - dt.timedelta(days=500),
            gndr="F",
            stock_group="Heifer",
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "green"
    assert health["max_days"] == 1


def test_health_unknown_when_no_cts_snapshot() -> None:
    session = _session()
    today = dt.date.today()
    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "unknown"
    assert health["label"] == "No CTS sync"
