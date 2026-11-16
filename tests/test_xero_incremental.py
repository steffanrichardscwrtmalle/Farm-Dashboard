"""Changes-only Xero sync still includes edits to old documents."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, XeroInvoice, XeroOrganisation
from app.services.xero_incremental import (
    invoices_modified_since,
    modified_since_header,
    next_modified_since,
)
from app.services.xero_invoices import fetch_invoices_page


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_modified_since_header_uses_xero_utc_format() -> None:
    header = modified_since_header(dt.datetime(2026, 3, 4, 5, 6, 7))
    assert header == {"If-Modified-Since": "2026-03-04T05:06:07"}
    assert modified_since_header(None) == {}


def test_recent_watermark_is_incremental_with_overlap() -> None:
    now = dt.datetime(2026, 6, 1, 12, 0, 0)
    since = next_modified_since(
        watermark=now - dt.timedelta(days=3),
        stored_count=10,
        latest_synced_at=now,
        page_cap=50,
        now=now,
    )
    assert since == now - dt.timedelta(days=5)


def test_old_watermark_forces_a_full_sync() -> None:
    now = dt.datetime(2026, 6, 1, 12, 0, 0)
    since = next_modified_since(
        watermark=now - dt.timedelta(days=30),
        stored_count=10,
        latest_synced_at=now,
        page_cap=50,
        now=now,
    )
    assert since is None


def test_existing_complete_history_does_not_start_from_scratch(db: Session) -> None:
    synced = dt.datetime(2026, 5, 1, 9, 0, 0)
    org = XeroOrganisation(tenant_id="tenant-a", tenant_name="Green Acre", dashboard_business="GAD")
    db.add(org)
    db.add(
        XeroInvoice(
            tenant_id="tenant-a",
            invoice_id="inv-1",
            invoice_type="ACCPAY",
            status="AUTHORISED",
            invoice_date=dt.date(2019, 4, 1),
            synced_at=synced,
        )
    )
    db.commit()

    since = invoices_modified_since(db, org, page_cap=50, now=dt.datetime(2026, 5, 2, 9, 0, 0))
    assert since == synced - dt.timedelta(days=2)


def test_capped_history_stays_a_full_sync(db: Session) -> None:
    org = XeroOrganisation(tenant_id="tenant-a", tenant_name="Green Acre", dashboard_business="GAD")
    db.add(org)
    db.add_all(
        [
            XeroInvoice(
                tenant_id="tenant-a",
                invoice_id=f"inv-{index}",
                invoice_type="ACCPAY",
                status="AUTHORISED",
                synced_at=dt.datetime(2026, 5, 1, 9, 0, 0),
            )
            for index in range(100)
        ]
    )
    db.commit()

    since = invoices_modified_since(db, org, page_cap=1, now=dt.datetime(2026, 5, 2, 9, 0, 0))
    assert since is None


def test_fetch_invoices_page_sends_modified_since() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("If-Modified-Since")
        return httpx.Response(200, json={"Invoices": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_invoices_page(
            client,
            access_token="token",
            tenant_id="tenant-a",
            page=1,
            modified_since=dt.datetime(2026, 1, 2, 3, 4, 5),
        )
    assert rows == []
    assert seen["header"] == "2026-01-02T03:04:05"
