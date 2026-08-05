"""Pending BCMS movement queue unit tests."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    CowEvent,
    CtsOnHolding,
    CtsReportedMovement,
    HerdBirth,
    HerdInventory,
    PedigreeRegistrationRecord,
    StockPurchaseAnimal,
)
from app.services.cts_movements import (
    archive_confirmed_movements,
    list_archived_movements,
    list_awaiting_cts_movements,
    list_pending_movements,
    mark_movements_reported,
    requeue_stale_awaiting_movements,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_list_pending_movements_buckets() -> None:
    session = _session()
    today = dt.date.today()

    # Sale: on CTS, not in inventory, with SOLD event
    session.add(
        CtsOnHolding(
            farm="CM",
            etag="UK111111111111",
            breed="HO",
            sex="F",
            dob=dt.date(2020, 1, 1),
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK111111111111",
            cow_id="101",
            event="SOLD",
            event_date=today - dt.timedelta(days=2),
            dest="Market",
            remark="Cull",
        )
    )
    session.add(
        PedigreeRegistrationRecord(
            farm="CM",
            etag="UK111111111111",
            dreg="UK111100000001",
            sreg="UK999999999999",
        )
    )

    # Birth: in inventory, not on CTS
    session.add(
        HerdBirth(
            farm="CM",
            etag="UK222222222222",
            cow_id="202",
            bdat=today - dt.timedelta(days=5),
            gndr="F",
            cbrd=1,
            category="Calf",
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK222222222222",
            cow_id="202",
            gender="F",
            sbrd="HF",
            dreg="UK222200000002",
            sreg="UK888888888888",
            bdat=today - dt.timedelta(days=5),
        )
    )

    # Birth record only (left inventory) must NOT appear
    session.add(
        HerdBirth(
            farm="CM",
            etag="UK666666666666",
            cow_id="606",
            bdat=today - dt.timedelta(days=3),
            gndr="F",
            cbrd=1,
        )
    )

    # Move-on: purchase in inventory, not on CTS
    session.add(
        StockPurchaseAnimal(
            farm="CM",
            etag="UK333333333333",
            edat=today - dt.timedelta(days=3),
            bdat=dt.date(2022, 6, 1),
            gndr="F",
            cbrd=121,
            stock_group="Heifer",
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK333333333333",
            cow_id="303",
            gender="F",
            category="Youngstock",
            bdat=dt.date(2022, 6, 1),
            sbrd="AA",
            cbrd=121,
            sreg="UK777777777777",
        )
    )

    # Inventory animal already on CTS must NOT appear
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK444444444444",
            cow_id="404",
            gender="F",
            bdat=today - dt.timedelta(days=1),
            sbrd="Holstein",
        )
    )
    session.add(
        CtsOnHolding(farm="CM", etag="UK444444444444", sex="F")
    )

    # Already reported inventory-only birth excluded
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK777777777770",
            cow_id="707",
            gender="F",
            bdat=today - dt.timedelta(days=4),
            sbrd="Holstein",
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="birth",
            etag="UK777777777770",
            event_date=today - dt.timedelta(days=4),
            status="sent",
        )
    )
    session.commit()

    result = list_pending_movements(session, "CM")
    types = {row["movement_type"] for row in result["rows"]}
    assert types == {"sale", "birth", "move_on"}
    assert result["counts"]["sale"] == 1
    assert result["counts"]["birth"] == 1
    assert result["counts"]["move_on"] == 1
    assert result["total"] == 3
    assert "UK666666666666" not in {r["etag"] for r in result["rows"]}

    sale = next(r for r in result["rows"] if r["movement_type"] == "sale")
    assert sale["etag"] == "UK111111111111"
    assert sale["breed"] == "HO"
    assert sale["dreg"] == "UK111100000001"
    assert sale["sreg"] == "UK999999999999"
    assert sale["days_since_event"] == 2

    birth = next(r for r in result["rows"] if r["movement_type"] == "birth")
    assert birth["etag"] == "UK222222222222"
    assert birth["breed"] == "HF"
    assert birth["dreg"] == "UK222200000002"
    assert birth["sreg"] == "UK888888888888"
    assert birth["days_since_event"] == 5

    move_on = next(r for r in result["rows"] if r["movement_type"] == "move_on")
    assert move_on["breed"] == "AAX"
    assert move_on["sreg"] == "UK777777777777"


def test_mark_movements_reported_drops_from_pending() -> None:
    session = _session()
    today = dt.date.today()
    session.add(
        CtsOnHolding(farm="CM", etag="UK555555555555", sex="F")
    )
    session.add(
        CowEvent(
            farm="CM",
            etag="UK555555555555",
            event="DIED",
            event_date=today,
            cow_id="505",
        )
    )
    session.commit()

    pending = list_pending_movements(session, "CM")
    assert pending["total"] == 1
    assert pending["rows"][0]["movement_type"] == "death"

    mark_movements_reported(session, farm="CM", items=pending["rows"])
    after = list_pending_movements(session, "CM")
    assert after["total"] == 0


def test_list_awaiting_cts_birth_until_on_holding() -> None:
    session = _session()
    today = dt.date.today()
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK222222222222",
            cow_id="202",
            gender="F",
            bdat=today - dt.timedelta(days=5),
            cbrd=101,
            dreg="UK222200000002",
            sreg="UK888888888888",
        )
    )
    session.add(
        HerdBirth(
            farm="CM",
            etag="UK222222222222",
            cow_id="202",
            bdat=today - dt.timedelta(days=5),
            gndr="F",
            cbrd=101,
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="birth",
            etag="UK222222222222",
            event_date=today - dt.timedelta(days=5),
            status="accepted",
            receipt="43719899",
        )
    )
    session.commit()

    awaiting = list_awaiting_cts_movements(session, "CM")
    assert awaiting["total"] == 1
    assert awaiting["rows"][0]["movement_type"] == "birth"
    assert awaiting["rows"][0]["receipt"] == "43719899"
    assert awaiting["counts"]["birth"] == 1

    # Once CTS holding catches up overnight, it moves to Archive.
    session.add(
        CtsOnHolding(
            farm="CM",
            etag="UK222222222222",
            breed="HF",
            sex="F",
            dob=today - dt.timedelta(days=5),
        )
    )
    session.commit()
    after = list_awaiting_cts_movements(session, "CM")
    assert after["total"] == 0
    archived = list_archived_movements(session, "CM")
    assert archived["total"] == 1
    assert archived["rows"][0]["etag"] == "UK222222222222"
    assert archived["rows"][0]["status"] == "archived"


def test_list_awaiting_cts_death_until_off_holding() -> None:
    session = _session()
    today = dt.date.today()
    session.add(CtsOnHolding(farm="CM", etag="UK555555555555", sex="F"))
    session.add(
        CowEvent(
            farm="CM",
            etag="UK555555555555",
            event="DIED",
            event_date=today,
            cow_id="505",
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="death",
            etag="UK555555555555",
            event_date=today,
            status="accepted",
            receipt="99",
        )
    )
    session.commit()

    awaiting = list_awaiting_cts_movements(session, "CM")
    assert awaiting["total"] == 1
    assert awaiting["rows"][0]["movement_type"] == "death"

    # Cleared from CTS holding → archived as confirmed.
    holding = session.scalar(
        select(CtsOnHolding).where(CtsOnHolding.etag == "UK555555555555")
    )
    assert holding is not None
    session.delete(holding)
    session.commit()
    after = list_awaiting_cts_movements(session, "CM")
    assert after["total"] == 0
    archived = list_archived_movements(session, "CM")
    assert archived["total"] == 1
    assert archived["rows"][0]["movement_type"] == "death"


def test_archive_confirmed_birth_promotes_from_awaiting() -> None:
    session = _session()
    today = dt.date.today()
    etag = "UK666666666666"
    session.add(
        HerdInventory(
            farm="CM",
            etag=etag,
            cow_id="606",
            gender="F",
            bdat=today - dt.timedelta(days=3),
            cbrd=101,
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="birth",
            etag=etag,
            event_date=today - dt.timedelta(days=3),
            status="accepted",
            receipt="123",
        )
    )
    session.add(
        CtsOnHolding(
            farm="CM",
            etag=etag,
            breed="HF",
            sex="F",
            dob=today - dt.timedelta(days=3),
        )
    )
    session.commit()

    result = archive_confirmed_movements(session, "CM")
    assert result["archived_count"] == 1
    assert list_awaiting_cts_movements(session, "CM")["total"] == 0
    archived = list_archived_movements(session, "CM")
    assert archived["total"] == 1
    assert archived["rows"][0]["receipt"] == "123"
    # Archived keys stay suppressed from pending even if inventory mismatches briefly.
    assert list_pending_movements(session, "CM")["total"] == 0


def test_requeue_stale_awaiting_birth_back_to_pending() -> None:
    session = _session()
    today = dt.date.today()
    etag = "UK222222222222"
    session.add(
        HerdInventory(
            farm="CM",
            etag=etag,
            cow_id="202",
            gender="F",
            bdat=today - dt.timedelta(days=5),
            cbrd=101,
        )
    )
    session.add(
        HerdBirth(
            farm="CM",
            etag=etag,
            cow_id="202",
            bdat=today - dt.timedelta(days=5),
            gndr="F",
            cbrd=101,
        )
    )
    reported = CtsReportedMovement(
        farm="CM",
        movement_type="birth",
        etag=etag,
        event_date=today - dt.timedelta(days=5),
        status="accepted",
        receipt="43719899",
        reported_at=dt.datetime.combine(
            today - dt.timedelta(days=1), dt.time(12, 0)
        ),
    )
    session.add(reported)
    session.commit()

    assert list_awaiting_cts_movements(session, "CM")["total"] == 1
    assert list_pending_movements(session, "CM")["total"] == 0

    result = requeue_stale_awaiting_movements(session, "CM", as_of=today)
    assert result["requeued_count"] == 1
    session.refresh(reported)
    assert reported.status == "failed"
    assert reported.error_message

    assert list_awaiting_cts_movements(session, "CM")["total"] == 0
    pending = list_pending_movements(session, "CM")
    assert pending["total"] == 1
    assert pending["rows"][0]["etag"] == etag
    assert pending["rows"][0]["movement_type"] == "birth"


def test_requeue_skips_same_uk_day_sends() -> None:
    session = _session()
    today = dt.date.today()
    etag = "UK333333333333"
    session.add(
        HerdInventory(
            farm="CM",
            etag=etag,
            cow_id="303",
            gender="F",
            bdat=today,
            cbrd=101,
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="birth",
            etag=etag,
            event_date=today,
            status="accepted",
            reported_at=dt.datetime.combine(today, dt.time(10, 0)),
        )
    )
    session.commit()

    result = requeue_stale_awaiting_movements(session, "CM", as_of=today)
    assert result["requeued_count"] == 0
    assert list_awaiting_cts_movements(session, "CM")["total"] == 1
    assert list_pending_movements(session, "CM")["total"] == 0


def test_requeue_skips_when_cts_already_reflects() -> None:
    session = _session()
    today = dt.date.today()
    etag = "UK444444444444"
    session.add(
        HerdInventory(
            farm="CM",
            etag=etag,
            cow_id="404",
            gender="F",
            bdat=today - dt.timedelta(days=5),
            cbrd=101,
        )
    )
    session.add(
        CtsOnHolding(
            farm="CM",
            etag=etag,
            breed="HF",
            sex="F",
            dob=today - dt.timedelta(days=5),
        )
    )
    session.add(
        CtsReportedMovement(
            farm="CM",
            movement_type="birth",
            etag=etag,
            event_date=today - dt.timedelta(days=5),
            status="accepted",
            reported_at=dt.datetime.combine(
                today - dt.timedelta(days=1), dt.time(12, 0)
            ),
        )
    )
    session.commit()

    # Sync path archives confirmed rows before requeue considers them.
    archived = archive_confirmed_movements(session, "CM")
    assert archived["archived_count"] == 1
    result = requeue_stale_awaiting_movements(session, "CM", as_of=today)
    assert result["requeued_count"] == 0
    assert list_awaiting_cts_movements(session, "CM")["total"] == 0
    assert list_archived_movements(session, "CM")["total"] == 1
