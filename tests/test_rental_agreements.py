"""Tests for rental agreements and monthly rent schedules."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.financial_data_sources import FINANCIAL_DATA_SOURCE_KEYS
from app.services.rental_agreements import (
    build_rental_agreements_report,
    build_rental_payment_chart,
    build_rental_payment_index,
    create_rental_agreement,
    deactivate_rental_agreement,
    rent_payment_due_date,
    save_rental_payments,
    update_rental_agreement,
)

FISCAL_YEAR = 2027


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_create_and_list_rental_agreement(db: Session) -> None:
    created = create_rental_agreement(
        db,
        business="CM",
        farm_name="Home Farm",
        farm_size=120.5,
    )
    assert created["business"] == "CM"
    assert created["farm_name"] == "Home Farm"
    assert created["farm_size"] == 120.5
    assert created["payment_day"] == 1

    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    assert report["fiscal_year"] == FISCAL_YEAR
    assert report["months"][0] == "2026-04-01"
    assert report["months"][-1] == "2027-03-01"
    assert len(report["agreements"]) == 1
    assert report["agreements"][0]["farm_name"] == "Home Farm"
    assert report["agreements"][0]["total"] is None
    assert report["agreements"][0]["per_acre"] is None


def test_monthly_payments_total_and_per_acre(db: Session) -> None:
    agreement = create_rental_agreement(
        db,
        business="GAD",
        farm_name="Top Field",
        farm_size=100,
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": 1000,
            },
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-05-01",
                "amount": 1500,
            },
        ],
    )

    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    row = report["agreements"][0]
    assert row["amounts"]["2026-04-01"] == 1000
    assert row["amounts"]["2026-05-01"] == 1500
    assert row["total"] == 2500
    assert row["per_acre"] == 25.0

    gad = report["business_totals"]["GAD"]
    assert gad["amounts"]["2026-04-01"] == 1000
    assert gad["amounts"]["2026-05-01"] == 1500
    assert gad["total"] == 2500
    assert report["business_totals"]["CM"]["total"] is None
    assert report["business_totals"]["Total"]["total"] == 2500


def test_business_totals_sum_across_agreements(db: Session) -> None:
    a = create_rental_agreement(db, business="CM", farm_name="A", farm_size=50)
    b = create_rental_agreement(db, business="CM", farm_name="B", farm_size=50)
    c = create_rental_agreement(db, business="GAD", farm_name="C", farm_size=50)
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {"agreement_id": a["id"], "payment_month": "2026-04-01", "amount": 100},
            {"agreement_id": b["id"], "payment_month": "2026-04-01", "amount": 200},
            {"agreement_id": c["id"], "payment_month": "2026-04-01", "amount": 50},
        ],
    )
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    assert report["business_totals"]["CM"]["amounts"]["2026-04-01"] == 300
    assert report["business_totals"]["GAD"]["amounts"]["2026-04-01"] == 50
    assert report["business_totals"]["Total"]["amounts"]["2026-04-01"] == 350


def test_update_and_deactivate_agreement(db: Session) -> None:
    created = create_rental_agreement(
        db, business="CM", farm_name="Old Name", farm_size=10
    )
    updated = update_rental_agreement(
        db,
        agreement_id=created["id"],
        business="GAD",
        farm_name="New Name",
        farm_size=20,
        payment_day=15,
    )
    assert updated["business"] == "GAD"
    assert updated["farm_name"] == "New Name"
    assert updated["farm_size"] == 20
    assert updated["payment_day"] == 15

    deactivate_rental_agreement(db, agreement_id=created["id"])
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    assert report["agreements"] == []


def test_clear_payment_removes_row(db: Session) -> None:
    agreement = create_rental_agreement(
        db, business="CM", farm_name="Clear Me", farm_size=10
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": 500,
            }
        ],
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": None,
            }
        ],
    )
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    assert report["agreements"][0]["amounts"]["2026-04-01"] is None
    assert report["agreements"][0]["total"] is None


def test_rental_payment_index_for_autofill(db: Session) -> None:
    agreement = create_rental_agreement(
        db, business="CM", farm_name="Index Farm", farm_size=10
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": 750,
            }
        ],
    )
    index = build_rental_payment_index(db, fiscal_year=FISCAL_YEAR)
    assert index[("CM", dt.date(2026, 4, 1))] == 750
    assert ("GAD", dt.date(2026, 4, 1)) not in index


def test_rents_data_source_registered() -> None:
    assert "rents.monthly_total" in FINANCIAL_DATA_SOURCE_KEYS


def test_rejects_invalid_business(db: Session) -> None:
    with pytest.raises(ValueError, match="Business must be"):
        create_rental_agreement(db, business="XYZ", farm_name="X", farm_size=1)


def test_allows_zero_acre_buildings(db: Session) -> None:
    created = create_rental_agreement(
        db,
        business="CM",
        farm_name="Cottage",
        farm_size=0,
    )
    assert created["farm_size"] == 0
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": created["id"],
                "payment_month": "2026-04-01",
                "amount": 900,
            }
        ],
    )
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    row = report["agreements"][0]
    assert row["total"] == 900
    assert row["per_acre"] is None


def test_rent_amounts_saved_to_two_decimal_places(db: Session) -> None:
    agreement = create_rental_agreement(
        db, business="CM", farm_name="Pence Field", farm_size=10
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": 1234.567,
            },
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-05-01",
                "amount": 100.1,
            },
        ],
    )
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    row = report["agreements"][0]
    assert row["amounts"]["2026-04-01"] == 1234.57
    assert row["amounts"]["2026-05-01"] == 100.10
    assert row["total"] == 1334.67
    assert report["business_totals"]["CM"]["amounts"]["2026-04-01"] == 1234.57


def test_rejects_negative_farm_size(db: Session) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        create_rental_agreement(db, business="CM", farm_name="Bad", farm_size=-1)


def test_payment_day_defaults_to_first_and_clamps_short_months() -> None:
    assert rent_payment_due_date(dt.date(2026, 8, 1), 1) == dt.date(2026, 8, 1)
    assert rent_payment_due_date(dt.date(2026, 8, 1), 15) == dt.date(2026, 8, 15)
    assert rent_payment_due_date(dt.date(2026, 2, 1), 31) == dt.date(2026, 2, 28)


def test_create_with_custom_payment_day(db: Session) -> None:
    created = create_rental_agreement(
        db,
        business="CM",
        farm_name="Late Rent",
        farm_size=10,
        payment_day=28,
    )
    assert created["payment_day"] == 28
    report = build_rental_agreements_report(db, fiscal_year=FISCAL_YEAR)
    assert report["agreements"][0]["payment_day"] == 28


def test_payment_chart_sums_from_fiscal_year_start(db: Session) -> None:
    cm = create_rental_agreement(db, business="CM", farm_name="Home Farm", farm_size=100)
    gad = create_rental_agreement(db, business="GAD", farm_name="Top Field", farm_size=50)
    save_rental_payments(
        db,
        fiscal_year=2026,
        rows=[
            {"agreement_id": cm["id"], "payment_month": "2026-03-01", "amount": 900},
        ],
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {"agreement_id": cm["id"], "payment_month": "2026-04-01", "amount": 1000},
            {"agreement_id": cm["id"], "payment_month": "2026-05-01", "amount": 1000},
            {"agreement_id": gad["id"], "payment_month": "2026-04-01", "amount": 400},
            {"agreement_id": gad["id"], "payment_month": "2026-05-01", "amount": 400},
        ],
    )
    chart = build_rental_payment_chart(db, as_of=dt.date(2026, 7, 1))
    assert chart["fiscal_year"] == 2027
    assert chart["from_month"] == "2026-04-01"
    assert chart["months"][0]["month_label"] == "Apr-26"
    assert chart["months"][0]["total"] == 1400.0
    assert chart["months"][0]["payment_count"] == 2
    assert chart["months"][1]["total"] == 1400.0

    cm_only = build_rental_payment_chart(db, as_of=dt.date(2026, 7, 1), business="CM")
    assert cm_only["months"][0]["total"] == 1000.0
    clipped = build_rental_payment_chart(
        db,
        as_of=dt.date(2026, 7, 1),
        business="CM",
        from_month="2026-05-01",
        to_month="2026-05-01",
    )
    assert [m["month_label"] for m in clipped["months"]] == ["May-26"]
    assert clipped["totals"]["total"] == 1000.0
