"""Aged payables summarised by contact and invoice month."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, XeroInvoice
from app.services.xero_aged_payables import list_aged_payables, resolve_aged_payable_view
from app.services.xero_invoices import _outstanding_amount


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _bill(
    *,
    invoice_id: str,
    contact_name: str,
    invoice_date: dt.date,
    amount_due: float,
    dashboard_business: str,
    invoice_type: str = "ACCPAY",
    status: str = "AUTHORISED",
    tenant_id: str = "tenant-a",
) -> XeroInvoice:
    return XeroInvoice(
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        invoice_type=invoice_type,
        status=status,
        contact_name=contact_name,
        invoice_date=invoice_date,
        amount_due=amount_due,
        total=amount_due,
        dashboard_business=dashboard_business,
    )


def test_outstanding_amount_uses_remaining_credit_for_notes() -> None:
    assert _outstanding_amount({"RemainingCredit": 25.5}) == 25.5
    assert _outstanding_amount({"AmountDue": 10, "RemainingCredit": 25.5}) == 10.0


def test_resolve_view_aliases() -> None:
    assert resolve_aged_payable_view("GAD")["businesses"] == ["Green Acre Dairy"]
    assert resolve_aged_payable_view("Green Acre Dairy")["id"] == "GAD"
    assert resolve_aged_payable_view("CM")["businesses"] == ["Cwrt Malle"]
    assert resolve_aged_payable_view("H&S")["businesses"] == ["H&S Forage"]
    assert resolve_aged_payable_view("CM + H&S")["businesses"] == [
        "Cwrt Malle",
        "H&S Forage",
    ]
    assert resolve_aged_payable_view("Cwrt Malle + H&S Forage")["id"] == "CM + H&S"


def test_list_aged_payables_pivots_contact_by_invoice_month(db: Session) -> None:
    db.add_all(
        [
            _bill(
                invoice_id="old",
                contact_name="Wynnstay",
                invoice_date=dt.date(2025, 12, 10),
                amount_due=100.0,
                dashboard_business="Green Acre Dairy",
            ),
            _bill(
                invoice_id="1",
                contact_name="Wynnstay",
                invoice_date=dt.date(2026, 1, 15),
                amount_due=1200.0,
                dashboard_business="Green Acre Dairy",
            ),
            _bill(
                invoice_id="2",
                contact_name="Wynnstay",
                invoice_date=dt.date(2026, 3, 1),
                amount_due=50.25,
                dashboard_business="Green Acre Dairy",
            ),
            _bill(
                invoice_id="3",
                contact_name="Prostock",
                invoice_date=dt.date(2026, 1, 15),
                amount_due=80.0,
                dashboard_business="Green Acre Dairy",
            ),
            _bill(
                invoice_id="4",
                contact_name="Wynnstay",
                invoice_date=dt.date(2026, 1, 28),
                amount_due=20.0,
                dashboard_business="Green Acre Dairy",
            ),
            _bill(
                invoice_id="paid",
                contact_name="Wynnstay",
                invoice_date=dt.date(2026, 2, 1),
                amount_due=0.0,
                dashboard_business="Green Acre Dairy",
                status="PAID",
            ),
            _bill(
                invoice_id="sale",
                contact_name="Customer",
                invoice_date=dt.date(2026, 1, 15),
                amount_due=999.0,
                dashboard_business="Green Acre Dairy",
                invoice_type="ACCREC",
            ),
            _bill(
                invoice_id="cm",
                contact_name="Wynnstay",
                invoice_date=dt.date(2026, 1, 15),
                amount_due=500.0,
                dashboard_business="Cwrt Malle",
                tenant_id="tenant-cm",
            ),
        ]
    )
    db.add(
        _bill(
            invoice_id="credit",
            contact_name="Prostock",
            invoice_date=dt.date(2026, 4, 10),
            amount_due=15.0,
            dashboard_business="Green Acre Dairy",
            invoice_type="ACCPAYCREDIT",
        )
    )
    db.commit()

    result = list_aged_payables(db, business="GAD", as_of=dt.date(2026, 4, 15))
    assert result["business"] == "GAD"
    assert result["months"] == [
        "2026-04-01",
        "2026-03-01",
        "2026-02-01",
        "2026-01-01",
        "older",
    ]
    assert result["month_labels"] == ["Apr-26", "Mar-26", "Feb-26", "Jan-26", "Older"]
    by_contact = {row["contact"]: row for row in result["contacts"]}
    assert by_contact["Wynnstay"]["amounts"]["older"] == 100.0
    assert by_contact["Wynnstay"]["amounts"]["2026-01-01"] == 1220.0
    assert by_contact["Wynnstay"]["amounts"]["2026-03-01"] == 50.25
    assert by_contact["Wynnstay"]["total"] == 1370.25
    assert by_contact["Prostock"]["amounts"]["2026-01-01"] == 80.0
    assert by_contact["Prostock"]["amounts"]["2026-04-01"] == -15.0
    assert by_contact["Prostock"]["total"] == 65.0
    assert "Customer" not in by_contact
    assert result["column_totals"]["older"] == 100.0
    assert result["grand_total"] == 1435.25


def test_cm_plus_hs_combines_matching_contacts(db: Session) -> None:
    db.add_all(
        [
            _bill(
                invoice_id="cm-1",
                contact_name="Mole Valley",
                invoice_date=dt.date(2026, 5, 2),
                amount_due=100.0,
                dashboard_business="Cwrt Malle",
                tenant_id="tenant-cm",
            ),
            _bill(
                invoice_id="hs-1",
                contact_name="Mole Valley",
                invoice_date=dt.date(2026, 5, 2),
                amount_due=40.0,
                dashboard_business="H&S Forage",
                tenant_id="tenant-hs",
            ),
            _bill(
                invoice_id="gad-1",
                contact_name="Mole Valley",
                invoice_date=dt.date(2026, 5, 2),
                amount_due=999.0,
                dashboard_business="Green Acre Dairy",
                tenant_id="tenant-gad",
            ),
        ]
    )
    db.commit()

    result = list_aged_payables(db, business="CM + H&S", as_of=dt.date(2026, 5, 20))
    assert result["months"] == [
        "2026-05-01",
        "2026-04-01",
        "2026-03-01",
        "2026-02-01",
        "older",
    ]
    assert result["contacts"][0]["contact"] == "Mole Valley"
    assert result["contacts"][0]["amounts"]["2026-05-01"] == 140.0
    assert result["grand_total"] == 140.0
