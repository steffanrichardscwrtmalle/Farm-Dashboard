"""Fetch and store Xero invoices/bills and credit notes."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.config import XERO_API_BASE_URL
from app.models import XeroInvoice, XeroInvoiceLine, XeroOrganisation
from app.services.xero_accounts import sync_accounts_for_organisation
from app.services.xero_amounts import normalize_line_amount_types
from app.services.xero_auth import XeroAuthError
from app.services.xero_dates import parse_xero_date, parse_xero_datetime
from app.services.xero_orgs import resolve_access_token

_INVOICE_TYPES = ("ACCREC", "ACCPAY")
_CREDIT_NOTE_TYPES = ("ACCRECCREDIT", "ACCPAYCREDIT")
# Bills, sales invoices, and supplier/customer credit notes used in P&L.
PNL_DOCUMENT_TYPES = _INVOICE_TYPES + _CREDIT_NOTE_TYPES
CREDIT_NOTE_TYPES = frozenset(_CREDIT_NOTE_TYPES)
SUMMARY_STATUSES = frozenset({"AUTHORISED", "PAID", "SUBMITTED"})
_SUMMARY_STATUSES = SUMMARY_STATUSES
_MAX_PAGES_PER_TENANT = 50


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _mapped_organisations(db: Session) -> list[XeroOrganisation]:
    return list(
        db.scalars(
            select(XeroOrganisation)
            .where(XeroOrganisation.is_active.is_(True))
            .where(XeroOrganisation.dashboard_business.isnot(None))
            .order_by(XeroOrganisation.tenant_name.asc())
        ).all()
    )


def fetch_invoices_page(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    page: int,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{XERO_API_BASE_URL}/Invoices",
        params={"page": page, "unitdp": 4},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        },
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise XeroAuthError(
            f"Failed to fetch invoices for tenant {tenant_id[:8]}… ({detail[:240]})"
        )
    payload = response.json() or {}
    invoices = payload.get("Invoices") or []
    return [inv for inv in invoices if inv.get("Type") in _INVOICE_TYPES]


def upsert_invoice(
    db: Session,
    *,
    tenant_id: str,
    dashboard_business: str | None,
    payload: dict[str, Any],
) -> None:
    invoice_id = str(payload.get("InvoiceID") or payload.get("CreditNoteID") or "").strip()
    if not invoice_id:
        return

    row = db.scalar(
        select(XeroInvoice)
        .where(XeroInvoice.tenant_id == tenant_id)
        .where(XeroInvoice.invoice_id == invoice_id)
        .options(selectinload(XeroInvoice.lines))
    )
    contact = payload.get("Contact") or {}
    now = _utcnow()
    values = {
        "invoice_number": (
            str(
                payload.get("InvoiceNumber")
                or payload.get("CreditNoteNumber")
                or ""
            ).strip()
            or None
        ),
        "invoice_type": str(payload.get("Type") or "").strip(),
        "status": (str(payload.get("Status") or "").strip() or None),
        "line_amount_types": normalize_line_amount_types(
            str(payload.get("LineAmountTypes") or "") or None
        ),
        "reference": (str(payload.get("Reference") or "").strip() or None),
        "contact_name": (str(contact.get("Name") or "").strip() or None),
        "currency_code": (str(payload.get("CurrencyCode") or "").strip() or None),
        "invoice_date": parse_xero_date(payload.get("Date")),
        "due_date": parse_xero_date(payload.get("DueDate")),
        "sub_total": float(payload["SubTotal"]) if payload.get("SubTotal") is not None else None,
        "total_tax": float(payload["TotalTax"]) if payload.get("TotalTax") is not None else None,
        "total": float(payload["Total"]) if payload.get("Total") is not None else None,
        "amount_due": float(payload["AmountDue"]) if payload.get("AmountDue") is not None else None,
        "amount_paid": (
            float(payload["AmountPaid"]) if payload.get("AmountPaid") is not None else None
        ),
        "dashboard_business": dashboard_business,
        "xero_updated_at": parse_xero_datetime(payload.get("UpdatedDateUTC")),
        "synced_at": now,
    }

    if row is None:
        row = XeroInvoice(tenant_id=tenant_id, invoice_id=invoice_id, **values)
        db.add(row)
        db.flush()
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.lines.clear()
        db.flush()

    for index, line in enumerate(payload.get("LineItems") or []):
        line_id = str(line.get("LineItemID") or "").strip() or f"{invoice_id}:{index}"
        db.add(
            XeroInvoiceLine(
                invoice_pk=row.id,
                tenant_id=tenant_id,
                line_item_id=line_id,
                description=(str(line.get("Description") or "").strip() or None),
                quantity=float(line["Quantity"]) if line.get("Quantity") is not None else None,
                unit_amount=(
                    float(line["UnitAmount"]) if line.get("UnitAmount") is not None else None
                ),
                line_amount=(
                    float(line["LineAmount"]) if line.get("LineAmount") is not None else None
                ),
                tax_amount=float(line["TaxAmount"]) if line.get("TaxAmount") is not None else None,
                account_code=(str(line.get("AccountCode") or "").strip() or None),
                account_id=(str(line.get("AccountID") or "").strip() or None),
                tax_type=(str(line.get("TaxType") or "").strip() or None),
            )
        )


def fetch_credit_notes_page(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    page: int,
) -> list[dict[str, Any]]:
    for attempt in range(6):
        response = client.get(
            f"{XERO_API_BASE_URL}/CreditNotes",
            params={"page": page, "unitdp": 4},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": tenant_id,
                "Accept": "application/json",
            },
        )
        if response.status_code == 429:
            time.sleep(2**attempt)
            continue
        if response.status_code >= 400:
            detail = response.text.strip()
            raise XeroAuthError(
                f"Failed to fetch credit notes for tenant {tenant_id[:8]}… ({detail[:240]})"
            )
        payload = response.json() or {}
        notes = payload.get("CreditNotes") or []
        return [note for note in notes if note.get("Type") in _CREDIT_NOTE_TYPES]
    raise XeroAuthError("Xero rate-limited credit note sync. Try again shortly.")


def fetch_credit_note(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    credit_note_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{XERO_API_BASE_URL}/CreditNotes/{credit_note_id}",
        params={"unitdp": 4},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        },
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise XeroAuthError(
            f"Failed to fetch credit note {credit_note_id[:8]}… ({detail[:240]})"
        )
    payload = response.json() or {}
    notes = payload.get("CreditNotes") or []
    return notes[0] if notes else {}


def sync_credit_notes_for_organisation(
    db: Session,
    client: httpx.Client,
    *,
    access_token: str,
    organisation: XeroOrganisation,
) -> dict[str, Any]:
    page = 1
    fetched = 0
    while page <= _MAX_PAGES_PER_TENANT:
        notes = fetch_credit_notes_page(
            client,
            access_token=access_token,
            tenant_id=organisation.tenant_id,
            page=page,
        )
        if not notes:
            break
        for payload in notes:
            credit_note_id = str(payload.get("CreditNoteID") or "").strip()
            lines = payload.get("LineItems") or []
            if credit_note_id and not lines:
                payload = (
                    fetch_credit_note(
                        client,
                        access_token=access_token,
                        tenant_id=organisation.tenant_id,
                        credit_note_id=credit_note_id,
                    )
                    or payload
                )
                time.sleep(0.35)
            upsert_invoice(
                db,
                tenant_id=organisation.tenant_id,
                dashboard_business=organisation.dashboard_business,
                payload=payload,
            )
            fetched += 1
        db.commit()
        if len(notes) < 100:
            break
        page += 1
    return {
        "tenant_id": organisation.tenant_id,
        "tenant_name": organisation.tenant_name,
        "dashboard_business": organisation.dashboard_business,
        "fetched": fetched,
        "pages": page,
    }


def sync_invoices_for_organisation(
    db: Session,
    client: httpx.Client,
    *,
    access_token: str,
    organisation: XeroOrganisation,
) -> dict[str, Any]:
    page = 1
    fetched = 0
    while page <= _MAX_PAGES_PER_TENANT:
        invoices = fetch_invoices_page(
            client,
            access_token=access_token,
            tenant_id=organisation.tenant_id,
            page=page,
        )
        if not invoices:
            break
        for payload in invoices:
            upsert_invoice(
                db,
                tenant_id=organisation.tenant_id,
                dashboard_business=organisation.dashboard_business,
                payload=payload,
            )
            fetched += 1
        db.commit()
        if len(invoices) < 100:
            break
        page += 1
    return {
        "tenant_id": organisation.tenant_id,
        "tenant_name": organisation.tenant_name,
        "dashboard_business": organisation.dashboard_business,
        "fetched": fetched,
        "pages": page,
    }


def sync_all_invoices(db: Session) -> dict[str, Any]:
    organisations = _mapped_organisations(db)
    if not organisations:
        raise XeroAuthError(
            "Map at least one Xero organisation to a dashboard business before syncing."
        )
    from app.services.xero_bank_transactions import sync_bank_transactions_for_organisation
    from app.services.xero_journals import sync_journals_for_organisation

    access_token = resolve_access_token(db)
    results: list[dict[str, Any]] = []
    credit_results: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    journal_results: list[dict[str, Any]] = []
    bank_results: list[dict[str, Any]] = []
    with httpx.Client(timeout=90.0) as client:
        for organisation in organisations:
            results.append(
                sync_invoices_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
            credit_results.append(
                sync_credit_notes_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
            account_results.append(
                sync_accounts_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
            journal_results.append(
                sync_journals_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
            bank_results.append(
                sync_bank_transactions_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
    summary = invoice_summary(db)
    return {
        "organisations": results,
        "fetched_total": sum(int(item["fetched"]) for item in results),
        "credit_notes": credit_results,
        "credit_notes_fetched_total": sum(int(item["fetched"]) for item in credit_results),
        "accounts": account_results,
        "accounts_fetched_total": sum(int(item["fetched"]) for item in account_results),
        "journals": journal_results,
        "journals_fetched_total": sum(int(item["fetched"]) for item in journal_results),
        "bank_transactions": bank_results,
        "bank_transactions_fetched_total": sum(
            int(item["fetched"]) for item in bank_results
        ),
        "summary": summary,
    }


def invoice_summary(db: Session) -> dict[str, Any]:
    total_count = db.scalar(select(func.count()).select_from(XeroInvoice)) or 0
    line_count = db.scalar(select(func.count()).select_from(XeroInvoiceLine)) or 0
    latest = db.scalar(select(func.max(XeroInvoice.synced_at)))

    rows = db.execute(
        select(
            XeroInvoice.dashboard_business,
            XeroInvoice.invoice_type,
            func.count(),
            func.coalesce(func.sum(XeroInvoice.total), 0.0),
        )
        .where(XeroInvoice.status.in_(list(_SUMMARY_STATUSES)))
        .group_by(XeroInvoice.dashboard_business, XeroInvoice.invoice_type)
        .order_by(XeroInvoice.dashboard_business.asc(), XeroInvoice.invoice_type.asc())
    ).all()

    by_business: dict[str, dict[str, Any]] = {}
    for business, invoice_type, count, total in rows:
        key = business or "Unmapped"
        bucket = by_business.setdefault(
            key,
            {
                "dashboard_business": key,
                "sales_count": 0,
                "sales_total": 0.0,
                "bills_count": 0,
                "bills_total": 0.0,
            },
        )
        if invoice_type in ("ACCREC", "ACCRECCREDIT"):
            bucket["sales_count"] = int(bucket["sales_count"]) + int(count)
            bucket["sales_total"] = float(bucket["sales_total"]) + float(total)
        elif invoice_type in ("ACCPAY", "ACCPAYCREDIT"):
            bucket["bills_count"] = int(bucket["bills_count"]) + int(count)
            bucket["bills_total"] = float(bucket["bills_total"]) + float(total)

    return {
        "invoice_count": int(total_count),
        "line_count": int(line_count),
        "last_synced_at": latest.isoformat() if latest else None,
        "by_business": list(by_business.values()),
        "included_statuses": sorted(_SUMMARY_STATUSES),
    }


def clear_invoices(db: Session) -> None:
    db.execute(delete(XeroInvoiceLine))
    db.execute(delete(XeroInvoice))
    db.commit()
