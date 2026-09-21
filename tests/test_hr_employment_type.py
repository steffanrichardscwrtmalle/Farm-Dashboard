"""Tests for self-employed HR enrollment (directory only, no contract)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYMENT_TYPE_EMPLOYED,
    EMPLOYMENT_TYPE_SELF_EMPLOYED,
    Base,
    Employee,
    User,
)
from app.services.hr_service import (
    HRServiceError,
    apply_cwrt_malle_leave_sheet,
    enroll_employee,
    normalize_employment_type,
    send_existing_employee,
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


@pytest.fixture()
def user(db):
    row = User(email="hr@test.local", password_hash="x", role="admin")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _payload(**overrides):
    data = {
        "business": "Cwrt Malle Ltd",
        "employment_type": "self_employed",
        "full_name": "Sam Contractor",
        "email": "sam@example.com",
        "role_title": "Farm Worker",
        "start_date": dt.date(2026, 8, 1),
        "pay_type": "hourly",
    }
    data.update(overrides)
    return data


def test_normalize_employment_type():
    assert normalize_employment_type("employed") == EMPLOYMENT_TYPE_EMPLOYED
    assert normalize_employment_type("self-employed") == EMPLOYMENT_TYPE_SELF_EMPLOYED
    assert normalize_employment_type("self employed") == EMPLOYMENT_TYPE_SELF_EMPLOYED
    assert normalize_employment_type("") == EMPLOYMENT_TYPE_EMPLOYED
    with pytest.raises(HRServiceError, match="Invalid employment type"):
        normalize_employment_type("contractor")


def test_enroll_self_employed_saves_to_directory_without_contract(db, user):
    result = enroll_employee(db, _payload(), user)
    employee = result["employee"]
    assert employee["employment_type"] == EMPLOYMENT_TYPE_SELF_EMPLOYED
    assert employee["employment_type_label"] == "Self-employed"
    assert employee["status"] == EMPLOYEE_STATUS_ACTIVE
    assert employee["full_name"] == "Sam Contractor"
    assert result["contract"] is None
    assert result["submission_id"] is None
    assert employee["contracts"] == []
    assert employee["holiday_hours_per_day"] == 8


def test_enroll_employed_requires_template(db, user):
    with pytest.raises(HRServiceError, match="contract template"):
        enroll_employee(db, _payload(employment_type="employed"), user)


def test_cannot_send_contract_to_self_employed(db, user):
    result = enroll_employee(db, _payload(), user)
    employee_id = result["employee"]["id"]
    with pytest.raises(HRServiceError, match="do not use employment contracts"):
        send_existing_employee(db, employee_id, None, user)


def test_format_sort_code():
    from app.services.hr_service import format_sort_code

    assert format_sort_code("123456") == "12-34-56"
    assert format_sort_code("12-34-56") == "12-34-56"
    assert format_sort_code("12 34 56") == "12-34-56"
    assert format_sort_code(None) is None
    assert format_sort_code("") is None


def test_enroll_saves_annual_leave_year_end(db, user):
    result = enroll_employee(
        db,
        _payload(
            holiday_year_end=dt.date(2026, 9, 30),
            annual_leave_days=28,
        ),
        user,
    )
    employee = result["employee"]
    assert employee["holiday_year_end"] == "2026-09-30"
    assert employee["annual_leave_days"] == 28


def test_enroll_defaults_annual_leave_days(db, user):
    result = enroll_employee(db, _payload(), user)
    assert result["employee"]["annual_leave_days"] == 0


def test_enroll_employed_draft_defaults_annual_leave_days(db, user):
    from app.services.hr_service import save_draft

    result = save_draft(db, _payload(employment_type="employed"), user)
    assert result["employee"]["annual_leave_days"] == 28


def test_apply_cwrt_malle_leave_sheet_updates_matching_staff(db, user):
    enroll_employee(
        db,
        _payload(employee_number="A170", email="a170@example.com"),
        user,
    )
    enroll_employee(
        db,
        _payload(employee_number="A022", email="a022@example.com"),
        user,
    )
    result = apply_cwrt_malle_leave_sheet(
        db,
        [
            {
                "employee_number": "A170",
                "accommodation_deduction": 80,
                "holidays_remaining": 7,
                "holiday_year_end": "2026-09-25",
            },
            {
                "employee_number": "A022",
                "accommodation_deduction": None,
                "holidays_remaining": 28,
                "holiday_year_end": "2027-01-21",
            },
            {"employee_number": "A999", "holidays_remaining": 4},
        ],
    )
    assert result["updated"] == ["A170", "A022"]
    assert result["missing"] == ["A999"]
    staff = {
        row.employee_number: row
        for row in db.scalars(select(Employee)).all()
    }
    assert staff["A170"].accommodation_deduction == 80
    assert staff["A170"].accommodation_cadence == "weekly"
    assert staff["A170"].holidays_remaining == 7
    assert staff["A170"].holiday_year_end == dt.date(2026, 9, 25)
    assert staff["A022"].accommodation_deduction is None
    assert staff["A022"].holidays_remaining == 28
    assert staff["A022"].holiday_year_end == dt.date(2027, 1, 21)
