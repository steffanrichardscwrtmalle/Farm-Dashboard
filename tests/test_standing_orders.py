"""Tests for standing orders."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, StandingOrder
from app.services.standing_orders import (
    create_standing_order,
    deactivate_standing_order,
    list_standing_orders,
    update_standing_order,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_create_lists_amount_and_remaining(db: Session) -> None:
    row = create_standing_order(
        db,
        name="Contractor",
        description="Slurry spreading",
        business="CM",
        amount=2500,
        months=12,
        payment_day=18,
        start_month="2025-04",
        user_id=1,
    )
    assert row["name"] == "Contractor"
    assert row["business"] == "CM"
    assert row["description"] == "Slurry spreading"
    assert row["amount"] == 2500.0
    assert row["months"] == 12
    assert row["payment_day"] == 18
    assert row["start_month"] == "2025-04-01"
    assert row["last_payment_label"] == "Mar-26"

    listed = list_standing_orders(db, as_of=dt.date(2025, 4, 10))
    assert len(listed) == 1
    assert listed[0]["months_remaining"] == 12
    assert listed[0]["amount_remaining"] == 30000.0


def test_update_and_deactivate(db: Session) -> None:
    created = create_standing_order(
        db,
        name="Vet",
        amount=400,
        months=24,
        payment_day=5,
        start_month=dt.date(2026, 1, 1),
        user_id=None,
    )
    updated = update_standing_order(
        db,
        order_id=created["id"],
        name="Vet retainer",
        amount=450,
        months=24,
        payment_day=10,
        start_month="2026-02",
        user_id=2,
    )
    assert updated["name"] == "Vet retainer"
    assert updated["amount"] == 450.0
    assert updated["payment_day"] == 10
    assert updated["start_month"] == "2026-02-01"

    deactivate_standing_order(db, order_id=created["id"])
    assert list_standing_orders(db) == []
    row = db.get(StandingOrder, created["id"])
    assert row is not None
    assert row.is_active is False


def test_duplicate_name_allowed(db: Session) -> None:
    first = create_standing_order(
        db,
        name="Contractor",
        amount=100,
        months=6,
        payment_day=1,
        start_month="2026-04",
        business="CM",
    )
    second = create_standing_order(
        db,
        name="contractor",
        amount=200,
        months=12,
        payment_day=2,
        start_month="2026-05",
        business="CM",
    )
    assert first["id"] != second["id"]
    listed = list_standing_orders(db)
    names = [row["name"] for row in listed]
    assert names.count("Contractor") == 1
    assert names.count("contractor") == 1


def test_other_frequency_every_seven_days(db: Session) -> None:
    from app.services.standing_orders import iter_standing_order_due_dates

    dues = iter_standing_order_due_dates(
        start_month=dt.date(2026, 4, 1),
        months=1,
        payment_day=1,
        frequency="other",
        interval_days=7,
    )
    assert dues[0] == dt.date(2026, 4, 1)
    assert dues[-1] == dt.date(2026, 4, 29)
    assert len(dues) == 5

    row = create_standing_order(
        db,
        name="Weekly contractor",
        amount=100,
        months=1,
        payment_day=1,
        start_month="2026-04",
        frequency="other",
        interval_days=7,
    )
    assert row["frequency"] == "other"
    assert row["interval_days"] == 7
    assert row["frequency_label"] == "Every 7 days"
    listed = list_standing_orders(db, as_of=dt.date(2026, 4, 1))
    # First payment is due today so it is treated as gone out
    assert listed[0]["payments_remaining"] == 4


def test_other_frequency_requires_interval_days(db: Session) -> None:
    with pytest.raises(ValueError, match="at least 1 day"):
        create_standing_order(
            db,
            name="Missing interval",
            amount=50,
            months=3,
            payment_day=1,
            start_month="2026-04",
            frequency="other",
        )
