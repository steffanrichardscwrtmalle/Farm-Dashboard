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


def test_duplicate_name_on_same_business_rejected(db: Session) -> None:
    create_standing_order(
        db,
        name="Contractor",
        amount=100,
        months=6,
        payment_day=1,
        start_month="2026-04",
        business="CM",
    )
    with pytest.raises(ValueError, match="already exists"):
        create_standing_order(
            db,
            name="contractor",
            amount=200,
            months=12,
            payment_day=2,
            start_month="2026-05",
            business="CM",
        )
    other = create_standing_order(
        db,
        name="Contractor",
        amount=200,
        months=12,
        payment_day=2,
        start_month="2026-05",
        business="GAD",
    )
    assert other["business"] == "GAD"
