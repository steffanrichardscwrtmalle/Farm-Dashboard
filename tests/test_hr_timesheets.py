"""Time sheet pay periods, active staff listing, and row saves."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ARCHIVED,
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
    EMPLOYMENT_TYPE_EMPLOYED,
    EMPLOYMENT_TYPE_SELF_EMPLOYED,
    PAY_TYPE_HOURLY,
    PAY_TYPE_SALARY,
    Base,
    Employee,
)
from app.services.crypto_fields import encrypt_field
from app.services.timesheet_service import (
    TimesheetError,
    current_period_start,
    list_periods,
    list_timesheet,
    period_containing,
    save_timesheet_row,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _employee(db, **overrides):
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


def test_cm_fortnight_starts_7_sep_2026():
    start, end = period_containing("CM", dt.date(2026, 9, 7))
    assert start == dt.date(2026, 9, 7)
    assert end == dt.date(2026, 9, 20)
    start, end = period_containing("CM", dt.date(2026, 9, 21))
    assert start == dt.date(2026, 9, 21)
    assert end == dt.date(2026, 10, 4)


def test_gad_month_starts_1_sep_2026():
    start, end = period_containing("GAD", dt.date(2026, 9, 21))
    assert start == dt.date(2026, 9, 1)
    assert end == dt.date(2026, 9, 30)
    start, end = period_containing("GAD", dt.date(2026, 10, 8))
    assert start == dt.date(2026, 10, 1)
    assert end == dt.date(2026, 10, 31)


def test_period_lists_include_cycle_starts():
    as_of = dt.date(2026, 9, 21)
    cm = list_periods("CM", as_of=as_of)
    gad = list_periods("GAD", as_of=as_of)
    assert cm[0]["start"] == "2026-09-07"
    assert cm[0]["end"] == "2026-09-20"
    assert [col["key"] for col in cm[0]["hours_columns"]] == [
        "hours_week_1",
        "hours_week_2",
    ]
    assert gad[0]["start"] == "2026-09-01"
    assert gad[0]["label"] == "September 2026"
    assert [col["key"] for col in gad[0]["hours_columns"]] == ["hours_week_1"]
    assert current_period_start("CM", as_of=as_of) == dt.date(2026, 9, 21)
    assert current_period_start("GAD", as_of=as_of) == dt.date(2026, 9, 1)


def test_lists_current_staff_including_onboarding(db):
    _employee(db, employee_number="CM001", full_name="Active Employed")
    _employee(
        db,
        employee_number="CM002",
        full_name="Active Contractor",
        email="sam@test.local",
        employment_type=EMPLOYMENT_TYPE_SELF_EMPLOYED,
    )
    _employee(
        db,
        employee_number="CM003",
        full_name="Onboarding",
        email="on@test.local",
        status=EMPLOYEE_STATUS_ONBOARDING,
    )
    _employee(
        db,
        employee_number="CM005",
        full_name="Pending Signature",
        email="pending@test.local",
        status=EMPLOYEE_STATUS_PENDING_SIGNATURE,
    )
    _employee(
        db,
        employee_number="CM004",
        full_name="Archived",
        email="left@test.local",
        status=EMPLOYEE_STATUS_ARCHIVED,
    )
    _employee(
        db,
        employee_number="GAD001",
        full_name="GAD Worker",
        email="gad@test.local",
        business="Green Acre Dairy Ltd",
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    names = [row["full_name"] for row in sheet["rows"]]
    assert names == [
        "Active Employed",
        "Onboarding",
        "Pending Signature",
        "Active Contractor",
    ]
    assert [row["employment_type_label"] for row in sheet["rows"]] == [
        "Employed",
        "Employed",
        "Employed",
        "Self-employed",
    ]
    assert sheet["rows"][0]["rate_label"] == "£12.50 / hr"
    assert sheet["cadence"] == "fortnightly"


def test_sorts_employed_hourly_then_salary_then_self_employed(db):
    _employee(
        db,
        employee_number="CM020",
        full_name="Zoe Hourly",
        email="zoe@test.local",
        pay_type=PAY_TYPE_HOURLY,
    )
    _employee(
        db,
        employee_number="CM021",
        full_name="Amy Salary",
        email="amy@test.local",
        pay_type=PAY_TYPE_SALARY,
    )
    _employee(
        db,
        employee_number="CM022",
        full_name="Ben Hourly",
        email="ben@test.local",
        pay_type=PAY_TYPE_HOURLY,
    )
    _employee(
        db,
        employee_number="CM023",
        full_name="Ada Contractor",
        email="ada@test.local",
        employment_type=EMPLOYMENT_TYPE_SELF_EMPLOYED,
        pay_type=PAY_TYPE_HOURLY,
    )
    _employee(
        db,
        employee_number="CM024",
        full_name="Cal Contractor",
        email="cal@test.local",
        employment_type=EMPLOYMENT_TYPE_SELF_EMPLOYED,
        pay_type=PAY_TYPE_SALARY,
    )
    sheet = list_timesheet(db, "CM", period_start=dt.date(2026, 9, 7))
    assert [row["full_name"] for row in sheet["rows"]] == [
        "Ben Hourly",
        "Zoe Hourly",
        "Amy Salary",
        "Ada Contractor",
        "Cal Contractor",
    ]


def test_salary_rate_shown_weekly(db):
    _employee(
        db,
        pay_type=PAY_TYPE_SALARY,
        pay_rate_enc=encrypt_field("26000"),
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 7), include_rate=True
    )
    assert sheet["rows"][0]["pay_type_label"] == "Salary"
    assert sheet["rows"][0]["rate"] == 500.0
    assert sheet["rows"][0]["rate_label"] == "£500.00 / wk"


def test_save_hours_and_holiday_year_total(db):
    staff = _employee(db)
    saved = save_timesheet_row(
        db,
        "CM",
        {
            "employee_id": staff.id,
            "period_start": dt.date(2026, 9, 7),
            "hours_week_1": 40,
            "hours_week_2": 32,
            "holiday_hours": 8,
            "dinner_break_hours": 5,
            "mileage": 22.5,
            "bonus": 50,
            "loan_deduction": 10,
            "other_deduction": 5,
            "accommodation_deduction": 80,
            "other_remark": "Covered weekend milking",
            "holidays_remaining": 72,
            "holidays_carry_forward": 8,
            "holiday_year_end": dt.date(2027, 3, 31),
        },
        include_rate=True,
    )
    assert saved["hours_week_1"] == 40
    assert saved["hours_week_2"] == 32
    assert saved["holiday_hours"] == 8
    assert saved["holidays_taken"] == 8
    assert saved["holidays_remaining"] == 72
    assert saved["other_remark"] == "Covered weekend milking"

    save_timesheet_row(
        db,
        "CM",
        {
            "employee_id": staff.id,
            "period_start": dt.date(2026, 9, 21),
            "holiday_hours": 16,
            "holiday_year_end": dt.date(2027, 3, 31),
            "holidays_remaining": 56,
            "holidays_carry_forward": 8,
        },
    )
    sheet = list_timesheet(
        db, "CM", period_start=dt.date(2026, 9, 21), include_rate=False
    )
    assert sheet["rows"][0]["holidays_taken"] == 24
    assert sheet["rows"][0]["rate"] is None


def test_rejects_wrong_farm_and_invalid_period(db):
    staff = _employee(db, business="Green Acre Dairy Ltd")
    with pytest.raises(TimesheetError, match="does not belong"):
        save_timesheet_row(
            db,
            "CM",
            {"employee_id": staff.id, "period_start": dt.date(2026, 9, 7)},
        )
    with pytest.raises(TimesheetError, match="first day of a pay period"):
        save_timesheet_row(
            db,
            "GAD",
            {"employee_id": staff.id, "period_start": dt.date(2026, 9, 7)},
        )


def test_can_save_onboarding_staff_but_not_archived(db):
    onboarding = _employee(
        db,
        employee_number="CM010",
        email="new@test.local",
        status=EMPLOYEE_STATUS_ONBOARDING,
    )
    saved = save_timesheet_row(
        db,
        "CM",
        {
            "employee_id": onboarding.id,
            "period_start": dt.date(2026, 9, 7),
            "hours_week_1": 20,
        },
    )
    assert saved["hours_week_1"] == 20

    archived = _employee(
        db,
        employee_number="CM011",
        email="gone@test.local",
        status=EMPLOYEE_STATUS_ARCHIVED,
    )
    with pytest.raises(TimesheetError, match="Archived"):
        save_timesheet_row(
            db,
            "CM",
            {"employee_id": archived.id, "period_start": dt.date(2026, 9, 7)},
        )
