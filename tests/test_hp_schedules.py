"""Tests for HP Schedules."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, HpSchedule
from app.services.hp_schedules import (
    _payment_due_date,
    build_hp_payment_chart,
    create_hp_schedule,
    deactivate_hp_schedule,
    list_hp_schedules,
    months_remaining,
    update_hp_schedule,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_weekend_due_dates_roll_to_monday() -> None:
    # 18 Apr 2026 is Saturday → Monday 20 Apr
    assert _payment_due_date(dt.date(2026, 4, 1), 0, 18) == dt.date(2026, 4, 20)
    # 19 Apr 2026 is Sunday → Monday 20 Apr
    assert _payment_due_date(dt.date(2026, 4, 1), 0, 19) == dt.date(2026, 4, 20)
    # Friday stays Friday
    assert _payment_due_date(dt.date(2026, 4, 1), 0, 17) == dt.date(2026, 4, 17)
    # 31 Jan 2026 is Saturday → Monday 2 Feb
    assert _payment_due_date(dt.date(2026, 1, 1), 0, 31) == dt.date(2026, 2, 2)
    start = dt.date(2025, 4, 1)
    # Before first payment day in April → all 12 remaining
    assert months_remaining(start_month=start, months=12, payment_day=18, as_of=dt.date(2025, 4, 10)) == 12
    # On/after first payment day → 11 remaining
    assert months_remaining(start_month=start, months=12, payment_day=18, as_of=dt.date(2025, 4, 18)) == 11
    # After final payment → 0
    assert months_remaining(start_month=start, months=12, payment_day=18, as_of=dt.date(2026, 3, 18)) == 0


def test_create_lists_monthly_amounts_and_remaining(db: Session) -> None:
    row =     create_hp_schedule(
        db,
        name="Tractor HP",
        description="New Holland T7",
        business="CM",
        monthly_capital=1000,
        monthly_interest=100,
        months=12,
        payment_day=18,
        start_month="2025-04",
        user_id=1,
    )
    assert row["name"] == "Tractor HP"
    assert row["business"] == "CM"
    assert row["description"] == "New Holland T7"
    assert row["monthly_capital"] == 1000.0
    assert row["monthly_interest"] == 100.0
    assert row["monthly_payment"] == 1100.0
    assert row["total_capital"] == 12000.0
    assert row["total_interest"] == 1200.0
    assert row["payment_day"] == 18
    assert row["start_month"] == "2025-04-01"
    assert row["last_payment_label"] == "Mar-26"

    listed = list_hp_schedules(db, as_of=dt.date(2025, 4, 10))
    assert len(listed) == 1
    assert listed[0]["months_remaining"] == 12
    assert listed[0]["capital_remaining"] == 12000.0


def test_update_and_deactivate(db: Session) -> None:
    created = create_hp_schedule(
        db,
        name="Mixer",
        monthly_capital=250,
        monthly_interest=25,
        months=24,
        payment_day=5,
        start_month=dt.date(2026, 1, 1),
        user_id=None,
    )
    updated = update_hp_schedule(
        db,
        schedule_id=created["id"],
        name="Mixer Wagon",
        monthly_capital=250,
        monthly_interest=25,
        months=24,
        payment_day=10,
        start_month="2026-02",
        user_id=2,
    )
    assert updated["name"] == "Mixer Wagon"
    assert updated["payment_day"] == 10
    assert updated["start_month"] == "2026-02-01"

    deactivate_hp_schedule(db, schedule_id=created["id"])
    assert list_hp_schedules(db) == []
    row = db.get(HpSchedule, created["id"])
    assert row is not None
    assert row.is_active is False


def test_payment_chart_sums_from_fiscal_year_start(db: Session) -> None:
    # FY2027 starts Apr 2026. Agreement starts Feb 2026 so first two months are excluded.
    create_hp_schedule(
        db,
        name="Chart HP",
        description="Test",
        business="CM",
        monthly_capital=100,
        monthly_interest=20,
        months=4,
        payment_day=10,
        start_month="2026-02",
    )
    create_hp_schedule(
        db,
        name="GAD HP",
        description="Other",
        business="GAD",
        monthly_capital=50,
        monthly_interest=0,
        months=4,
        payment_day=10,
        start_month="2026-02",
    )
    chart = build_hp_payment_chart(db, as_of=dt.date(2026, 7, 1))
    assert chart["fiscal_year"] == 2027
    assert chart["from_month"] == "2026-04-01"
    labels = [m["month_label"] for m in chart["months"]]
    assert labels[0] == "Apr-26"
    # Feb+Mar excluded; Apr+May included for both businesses
    assert chart["months"][0]["total"] == 170.0
    assert chart["months"][1]["total"] == 170.0

    cm_only = build_hp_payment_chart(db, as_of=dt.date(2026, 7, 1), business="CM")
    assert cm_only["months"][0]["total"] == 120.0
    clipped = build_hp_payment_chart(
        db,
        as_of=dt.date(2026, 7, 1),
        business="CM",
        from_month="2026-05-01",
        to_month="2026-05-01",
    )
    assert [m["month_label"] for m in clipped["months"]] == ["May-26"]
    assert clipped["totals"]["total"] == 120.0

    create_hp_schedule(
        db,
        name="Shed HP",
        monthly_capital=100,
        monthly_interest=10,
        months=10,
        payment_day=1,
        start_month="2026-01",
    )
    with pytest.raises(ValueError, match="already exists"):
        create_hp_schedule(
            db,
            name="shed hp",
            monthly_capital=200,
            monthly_interest=20,
            months=12,
            payment_day=2,
            start_month="2026-02",
        )
