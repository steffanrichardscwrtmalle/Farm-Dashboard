"""Tests for self-employed HR enrollment (directory only, no contract)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYMENT_TYPE_EMPLOYED,
    EMPLOYMENT_TYPE_SELF_EMPLOYED,
    Base,
    User,
)
from app.services.hr_service import (
    HRServiceError,
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


def test_enroll_employed_requires_template(db, user):
    with pytest.raises(HRServiceError, match="contract template"):
        enroll_employee(db, _payload(employment_type="employed"), user)


def test_cannot_send_contract_to_self_employed(db, user):
    result = enroll_employee(db, _payload(), user)
    employee_id = result["employee"]["id"]
    with pytest.raises(HRServiceError, match="do not use employment contracts"):
        send_existing_employee(db, employee_id, None, user)
