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


def test_death_yellow_at_three_days_red_at_six() -> None:
    session = _session()
    today = dt.date.today()
    session.add(CtsOnHolding(farm="CM", etag="UK444444444444", sex="F"))
    session.add(
        CowEvent(
            farm="CM",
            etag="UK444444444444",
            event="DIED",
            event_date=today - dt.timedelta(days=3),
            cow_id="4",
        )
    )
    session.commit()
    assert get_bcms_health(session, farms=["CM"], as_of=today)["status"] == "yellow"

    session2 = _session()
    session2.add(CtsOnHolding(farm="CM", etag="UK444444444445", sex="F"))
    session2.add(
        CowEvent(
            farm="CM",
            etag="UK444444444445",
            event="DIED",
            event_date=today - dt.timedelta(days=6),
            cow_id="5",
        )
    )
    session2.commit()
    assert get_bcms_health(session2, farms=["CM"], as_of=today)["status"] == "red"


def test_birth_yellow_at_three_days_red_at_six() -> None:
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
    session.commit()
    assert get_bcms_health(session, farms=["CM"], as_of=today)["status"] == "yellow"

    session2 = _session()
    session2.add(
        HerdInventory(
            farm="CM",
            etag="UK555555555556",
            cow_id="6",
            gender="F",
            bdat=today - dt.timedelta(days=6),
        )
    )
    session2.commit()
    assert get_bcms_health(session2, farms=["CM"], as_of=today)["status"] == "red"


def test_sale_yellow_at_two_days_red_at_three() -> None:
    session = _session()
    today = dt.date.today()
    session.add(CtsOnHolding(farm="CM", etag="UK444444444444", sex="F"))
    session.add(
        CowEvent(
            farm="CM",
            etag="UK444444444444",
            event="SOLD",
            event_date=today - dt.timedelta(days=2),
            cow_id="4",
        )
    )
    session.commit()
    assert get_bcms_health(session, farms=["CM"], as_of=today)["status"] == "yellow"

    session2 = _session()
    session2.add(CtsOnHolding(farm="CM", etag="UK444444444445", sex="F"))
    session2.add(
        CowEvent(
            farm="CM",
            etag="UK444444444445",
            event="SOLD",
            event_date=today - dt.timedelta(days=3),
            cow_id="5",
        )
    )
    session2.commit()
    assert get_bcms_health(session2, farms=["CM"], as_of=today)["status"] == "red"


def test_move_on_yellow_at_two_days_red_at_three() -> None:
    session = _session()
    today = dt.date.today()
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
            edat=today - dt.timedelta(days=2),
            bdat=today - dt.timedelta(days=500),
            gndr="F",
            stock_group="Heifer",
        )
    )
    session.commit()
    assert get_bcms_health(session, farms=["CM"], as_of=today)["status"] == "yellow"

    session2 = _session()
    session2.add(
        HerdInventory(
            farm="CM",
            etag="UK777777777778",
            cow_id="8",
            gender="F",
            bdat=today - dt.timedelta(days=500),
        )
    )
    session2.add(
        StockPurchaseAnimal(
            farm="CM",
            etag="UK777777777778",
            edat=today - dt.timedelta(days=3),
            bdat=today - dt.timedelta(days=500),
            gndr="F",
            stock_group="Heifer",
        )
    )
    session2.commit()
    assert get_bcms_health(session2, farms=["CM"], as_of=today)["status"] == "red"


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


def test_health_green_for_cts_only_while_events_lag_inventory() -> None:
    """Sold in DC after events pull: inventory drops them before events catch up."""
    session = _session()
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    session.add(
        CtsOnHolding(farm="CM", etag="UK111111111111", sex="F")
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK111111111111",
            cow_id="1",
            gender="F",
            bdat=today - dt.timedelta(days=400),
            import_timestamp=dt.datetime.combine(today, dt.time(7, 0)),
        )
    )
    session.add(
        CtsOnHolding(farm="CM", etag="UK666666666666", sex="F")
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK111111111111",
            event="FRESH",
            event_date=yesterday,
            cow_id="1",
            import_timestamp=dt.datetime.combine(yesterday, dt.time(6, 0)),
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "green"
    assert health["max_days"] == 1
    assert health["mismatch_count"] == 1
    assert health["farms"][0]["status"] == "green"


def test_health_red_for_cts_only_when_events_are_fresh() -> None:
    """Once events have caught up with inventory, missing exits are unexplained."""
    session = _session()
    today = dt.date.today()
    session.add(
        CtsOnHolding(farm="CM", etag="UK111111111111", sex="F")
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK111111111111",
            cow_id="1",
            gender="F",
            bdat=today - dt.timedelta(days=400),
            import_timestamp=dt.datetime.combine(today, dt.time(7, 0)),
        )
    )
    session.add(
        CtsOnHolding(farm="CM", etag="UK666666666666", sex="F")
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK111111111111",
            event="FRESH",
            event_date=today,
            cow_id="1",
            import_timestamp=dt.datetime.combine(today, dt.time(8, 0)),
        )
    )
    session.commit()

    health = get_bcms_health(session, farms=["CM"], as_of=today)
    assert health["status"] == "red"
    assert health["mismatch_count"] == 1


def test_health_uses_purchase_date_for_move_on() -> None:
    session = _session()
    today = dt.date.today()
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


def test_worst_mismatch_wins_across_kinds() -> None:
    session = _session()
    today = dt.date.today()
    # Birth at 3 days → yellow on its own
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK333333333333",
            cow_id="3",
            gender="F",
            bdat=today - dt.timedelta(days=3),
        )
    )
    # Sale at 3 days → red, so farm should be red
    session.add(CtsOnHolding(farm="CM", etag="UK222222222222", sex="F"))
    session.add(
        CowEvent(
            farm="CM",
            etag="UK222222222222",
            event="SOLD",
            event_date=today - dt.timedelta(days=3),
            cow_id="2",
        )
    )
    session.commit()
    assert get_bcms_health(session, farms=["CM"], as_of=today)["status"] == "red"
