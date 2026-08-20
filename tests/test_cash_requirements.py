"""Tests for the Cash requirements HP + rent payment list."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.cash_requirements import build_cash_requirements_report
from app.services.hp_schedules import create_hp_schedule
from app.services.rental_agreements import create_rental_agreement, save_rental_payments


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _seed(db: Session) -> None:
    create_hp_schedule(
        db,
        name="Tractor HP",
        business="CM",
        monthly_capital=1000,
        monthly_interest=100,
        months=12,
        payment_day=18,
        start_month="2026-04",
    )
    create_hp_schedule(
        db,
        name="Mixer HP",
        business="GAD",
        monthly_capital=400,
        monthly_interest=50,
        months=12,
        payment_day=5,
        start_month="2026-04",
    )
    rent = create_rental_agreement(
        db,
        business="CM",
        farm_name="Home Farm",
        farm_size=100,
    )
    save_rental_payments(
        db,
        fiscal_year=2027,
        rows=[
            {
                "agreement_id": rent["id"],
                "payment_month": "2026-08-01",
                "amount": 2500,
            }
        ],
    )


def test_lists_this_month_payments_in_date_order(db: Session) -> None:
    _seed(db)
    report = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 10)
    )
    assert [row["due_date"] for row in report["payments"]] == [
        "2026-08-01",
        "2026-08-05",
        "2026-08-18",
    ]
    assert [row["name"] for row in report["payments"]] == [
        "Home Farm",
        "Mixer HP",
        "Tractor HP",
    ]
    assert report["payments"][0]["amount"] == 2500
    assert report["payments"][2]["amount"] == 1100


def test_past_due_is_paid_future_is_outstanding(db: Session) -> None:
    _seed(db)
    report = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 10)
    )
    by_name = {row["name"]: row for row in report["payments"]}
    assert by_name["Home Farm"]["paid"] is True
    assert by_name["Mixer HP"]["paid"] is True
    assert by_name["Tractor HP"]["paid"] is False
    assert report["totals"]["paid"] == 2500 + 450
    assert report["totals"]["remaining"] == 1100
    assert report["totals"]["total"] == 2500 + 450 + 1100

    on_due_day = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 18)
    )
    tractor = next(row for row in on_due_day["payments"] if row["name"] == "Tractor HP")
    assert tractor["paid"] is True
    assert on_due_day["totals"]["remaining"] == 0


def test_current_month_chart_remaining_drops_after_due_dates(db: Session) -> None:
    _seed(db)
    before = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 1)
    )
    after = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 20)
    )
    august_before = next(m for m in before["months"] if m["month"] == "2026-08-01")
    august_after = next(m for m in after["months"] if m["month"] == "2026-08-01")
    assert august_before["is_current"] is True
    assert august_before["total"] == august_after["total"]
    assert august_before["remaining"] > august_after["remaining"]
    assert august_after["remaining"] == 0
    assert august_after["paid"] == august_after["total"]


def test_business_filter_excludes_other_farm(db: Session) -> None:
    _seed(db)
    report = build_cash_requirements_report(
        db, business="CM", month="2026-08", today=dt.date(2026, 8, 10)
    )
    names = [row["name"] for row in report["payments"]]
    assert names == ["Home Farm", "Tractor HP"]
    assert all(row["business"] == "CM" for row in report["payments"])


def test_hp_weekend_due_date_moves_to_monday(db: Session) -> None:
    create_hp_schedule(
        db,
        name="Weekend HP",
        business="CM",
        monthly_capital=800,
        monthly_interest=0,
        months=12,
        payment_day=1,
        start_month="2026-04",
    )
    report = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 2)
    )
    weekend = next(row for row in report["payments"] if row["name"] == "Weekend HP")
    # 1 Aug 2026 is Saturday → Monday 3 Aug
    assert weekend["due_date"] == "2026-08-03"
    assert weekend["paid"] is False


def test_rent_uses_agreement_payment_day(db: Session) -> None:
    rent = create_rental_agreement(
        db,
        business="CM",
        farm_name="Mid month",
        farm_size=10,
        payment_day=15,
    )
    save_rental_payments(
        db,
        fiscal_year=2027,
        rows=[
            {
                "agreement_id": rent["id"],
                "payment_month": "2026-08-01",
                "amount": 900,
            }
        ],
    )
    report = build_cash_requirements_report(
        db, month="2026-08", today=dt.date(2026, 8, 10)
    )
    row = next(item for item in report["payments"] if item["name"] == "Mid month")
    assert row["due_date"] == "2026-08-15"
    assert row["paid"] is False
