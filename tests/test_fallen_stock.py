"""Tests for fallen stock Office Admin workflow."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, FallenStockRecord, User
from app.services.fallen_stock import (
    confirm_collections,
    list_fallen_stock,
    unarchive_collections,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(
        email="test@example.com",
        password_hash="x",
        role="editor",
        permissions='{"pages":["office_admin"],"actions":["office_admin.fallen_stock"]}',
    )
    session.add(user)
    session.add(
        CowEvent(
            farm="GAD",
            cow_id="1001",
            etag="UK752261100001",
            event="DIED",
            event_date=dt.date(2025, 6, 1),
            dest="RENDERER",
            remark="SICK",
            gndr="F",
            bdat=dt.date(2023, 1, 1),
        )
    )
    session.add(
        CowEvent(
            farm="GAD",
            cow_id="1002",
            etag="UK752261100002",
            event="SOLD",
            event_date=dt.date(2025, 6, 2),
            dest="MARKET",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2022, 1, 1),
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="2001",
            etag="UK752261200001",
            event="DIED",
            event_date=dt.date(2025, 6, 3),
            dest="RENDERER",
            remark="OTHER",
            gndr="F",
            bdat=dt.date(2021, 6, 1),
        )
    )
    session.commit()
    yield session
    session.close()


def test_list_fallen_stock_returns_only_died_events(db: Session) -> None:
    result = list_fallen_stock(db, farms=["GAD", "CM"])
    etags = {row["etag"] for row in result["rows"]}
    assert etags == {"UK752261100001", "UK752261200001"}
    assert result["total"] == 2


def test_confirm_and_unarchive_round_trip(db: Session) -> None:
    user = db.query(User).one()
    active = list_fallen_stock(db, farms=["GAD"], status="active")
    assert len(active["rows"]) == 1
    key = active["rows"][0]["record_key"]

    confirm_collections(db, [key], user)

    active_after = list_fallen_stock(db, farms=["GAD"], status="active")
    archived = list_fallen_stock(db, farms=["GAD"], status="archived")
    assert active_after["total"] == 0
    assert archived["total"] == 1
    assert archived["rows"][0]["remark"] == "SICK"

    unarchive_collections(db, [key], user)

    active_restored = list_fallen_stock(db, farms=["GAD"], status="active")
    archived_after = list_fallen_stock(db, farms=["GAD"], status="archived")
    assert active_restored["total"] == 1
    assert archived_after["total"] == 0

    record = db.query(FallenStockRecord).one()
    assert record.archived_at is None
    assert record.collected_at is None
    assert record.unarchived_at is not None
