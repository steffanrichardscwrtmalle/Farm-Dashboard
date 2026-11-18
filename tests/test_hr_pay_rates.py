"""Dated pay changes leave earlier time sheets on the old rate."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYMENT_TYPE_EMPLOYED,
    PAY_TYPE_HOURLY,
    PAY_TYPE_SALARY,
    Base,
    Employee,
    User,
)
from app.services.crypto_fields import decrypt_field, encrypt_field
from app.services.hr_service import HRServiceError, record_pay_change, update_employee
from app.services.timesheet_service import list_timesheet


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db) -> User:
    user = User(email="hr@test.local", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _employee(db, **overrides) -> Employee:
    data = {
        "business": "Cwrt Malle Ltd",
        "employment_type": EMPLOYMENT_TYPE_EMPLOYED,
        "employee_number": "CM001",
        "full_name": "Alex Farmhand",
        "email": "alex@test.local",
        "pay_type": PAY_TYPE_HOURLY,
        "pay_rate_enc": encrypt_field("12.50"),
        "role_title": "Farm Worker",
        "start_date": dt.date(2026, 1, 1),
        "status": EMPLOYEE_STATUS_ACTIVE,
    }
    data.update(overrides)
    row = Employee(**data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _edit_payload(employee: Employee, **overrides) -> dict:
    payload = {
        "business": employee.business,
        "employment_type": employee.employment_type,
        "full_name": employee.full_name,
        "email": employee.email,
        "pay_type": employee.pay_type,
        "pay_rate": decrypt_field(employee.pay_rate_enc),
        "role_title": employee.role_title,
        "start_date": employee.start_date,
    }
    payload.update(overrides)
    return payload


def test_later_rate_does_not_change_earlier_timesheet(db):
    staff = _employee(db)
    user = _user(db)
    record_pay_change(
        db,
        staff.id,
        pay_rate="14.00",
        effective_from=dt.date(2026, 9, 21),
        user=user,
        include_sensitive=True,
    )
    before = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    after = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 21), include_rate=True
    )
    assert before["rows"][0]["rate"] == 12.5
    assert before["rows"][0]["rate_label"] == "£12.50 / hr"
    assert after["rows"][0]["rate"] == 14.0
    assert after["rows"][0]["rate_label"] == "£14.00 / hr"
    db.refresh(staff)
    in_force = "14.00" if dt.date.today() >= dt.date(2026, 9, 21) else "12.50"
    assert decrypt_field(staff.pay_rate_enc) == in_force


def test_rate_change_inside_a_fortnight_is_labelled(db):
    staff = _employee(db)
    user = _user(db)
    record_pay_change(
        db,
        staff.id,
        pay_rate="14",
        effective_from=dt.date(2026, 9, 10),
        user=user,
        include_sensitive=True,
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    assert sheet["rows"][0]["rate_label"] == (
        "£12.50 / hr until 09/09/2026 · £14.00 / hr from 10/09/2026"
    )
    assert sheet["rows"][0]["rate"] == 12.5


def test_salary_weeks_follow_the_rate_in_force(db):
    staff = _employee(
        db,
        pay_type=PAY_TYPE_SALARY,
        pay_rate_enc=encrypt_field("26000"),
    )
    user = _user(db)
    record_pay_change(
        db,
        staff.id,
        pay_rate="31200",
        effective_from=dt.date(2026, 9, 14),
        user=user,
        include_sensitive=True,
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    row = sheet["rows"][0]
    assert row["hours_week_1"] == 500.0
    assert row["hours_week_2"] == 600.0
    assert "£26,000.00 / yr until 13/09/2026" in row["rate_label"]
    assert "£31,200.00 / yr from 14/09/2026" in row["rate_label"]


def test_profile_edit_cannot_rewrite_a_recorded_change(db):
    staff = _employee(db)
    user = _user(db)
    record_pay_change(
        db,
        staff.id,
        pay_rate="14.00",
        effective_from=dt.date(2026, 9, 21),
        user=user,
    )
    with pytest.raises(HRServiceError, match="Use Change"):
        update_employee(
            db,
            staff.id,
            _edit_payload(staff, pay_rate="15.00"),
            user,
        )
    before = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    assert before["rows"][0]["rate"] == 12.5


def test_same_rate_on_the_change_date_is_rejected(db):
    staff = _employee(db)
    user = _user(db)
    with pytest.raises(HRServiceError, match="already in place"):
        record_pay_change(
            db,
            staff.id,
            pay_rate="12.50",
            effective_from=dt.date(2026, 6, 1),
            user=user,
        )


def test_future_rate_leaves_current_and_past_periods_unchanged(db):
    staff = _employee(db)
    user = _user(db)
    record_pay_change(
        db,
        staff.id,
        pay_rate="15.00",
        effective_from=dt.date(2099, 1, 1),
        user=user,
        include_sensitive=True,
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    assert sheet["rows"][0]["rate"] == 12.5
    db.refresh(staff)
    assert decrypt_field(staff.pay_rate_enc) == "12.50"


def test_change_before_start_date_is_rejected(db):
    staff = _employee(db)
    user = _user(db)
    with pytest.raises(HRServiceError, match="start date"):
        record_pay_change(
            db,
            staff.id,
            pay_rate="13.00",
            effective_from=dt.date(2025, 12, 1),
            user=user,
        )
