"""Fetch and store Xero Spend/Receive Money bank transactions for P&L actuals."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.config import XERO_API_BASE_URL
from app.models import XeroBankTransaction, XeroBankTransactionLine, XeroOrganisation
from app.services.xero_auth import XeroAuthError
from app.services.xero_dates import parse_xero_date, parse_xero_datetime
from app.services.xero_orgs import resolve_access_token

# Authorised spend/receive money (and over/prepayments). Transfers are bank↔bank only.
BANK_STATUSES = frozenset({"AUTHORISED"})
_INCLUDED_TYPE_PREFIXES = ("SPEND", "RECEIVE")
_EXCLUDED_TYPE_FRAGMENTS = ("TRANSFER",)
_MAX_PAGES = 100


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def bank_type_as_invoice_type(transaction_type: str | None) -> str | None:
    """Map bank type to ACCPAY/ACCREC signing used elsewhere in P&L/actuals."""
    raw = (transaction_type or "").strip().upper()
    if not raw:
        return None
    if any(fragment in raw for fragment in _EXCLUDED_TYPE_FRAGMENTS):
        return None
    if raw.startswith("SPEND"):
        return "ACCPAY"
    if raw.startswith("RECEIVE"):
        return "ACCREC"
    return None


def is_pnl_bank_type(transaction_type: str | None) -> bool:
    return bank_type_as_invoice_type(transaction_type) is not None


def _mapped_organisations(db: Session) -> list[XeroOrganisation]:
    return list(
        db.scalars(
            select(XeroOrganisation)
            .where(XeroOrganisation.is_active.is_(True))
            .where(XeroOrganisation.dashboard_business.isnot(None))
            .order_by(XeroOrganisation.tenant_name.asc())
        ).all()
    )


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    access_token: str,
    tenant_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for attempt in range(6):
        response = client.get(
            url,
            params=params,
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
                f"Failed to fetch bank transactions for tenant {tenant_id[:8]}… ({detail[:240]})"
            )
        return response.json() or {}
    raise XeroAuthError("Xero rate-limited bank transaction sync. Try again shortly.")


def fetch_bank_transactions_page(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    page: int,
) -> list[dict[str, Any]]:
    payload = _get_json(
        client,
        f"{XERO_API_BASE_URL}/BankTransactions",
        access_token=access_token,
        tenant_id=tenant_id,
        # page= is required for LineItems to be returned.
        params={"page": page, "unitdp": 4},
    )
    return list(payload.get("BankTransactions") or [])


def fetch_bank_transaction(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    bank_transaction_id: str,
) -> dict[str, Any]:
    payload = _get_json(
        client,
        f"{XERO_API_BASE_URL}/BankTransactions/{bank_transaction_id}",
        access_token=access_token,
        tenant_id=tenant_id,
    )
    rows = payload.get("BankTransactions") or []
    return rows[0] if rows else {}


def upsert_bank_transaction(
    db: Session,
    *,
    tenant_id: str,
    dashboard_business: str | None,
    payload: dict[str, Any],
) -> None:
    bank_transaction_id = str(payload.get("BankTransactionID") or "").strip()
    if not bank_transaction_id:
        return

    row = db.scalar(
        select(XeroBankTransaction)
        .where(XeroBankTransaction.tenant_id == tenant_id)
        .where(XeroBankTransaction.bank_transaction_id == bank_transaction_id)
        .options(selectinload(XeroBankTransaction.lines))
    )
    contact = payload.get("Contact") or {}
    now = _utcnow()
    values = {
        "transaction_type": str(payload.get("Type") or "").strip(),
        "status": (str(payload.get("Status") or "").strip() or None),
        "reference": (str(payload.get("Reference") or "").strip() or None),
        "contact_name": (str(contact.get("Name") or "").strip() or None),
        "currency_code": (str(payload.get("CurrencyCode") or "").strip() or None),
        "transaction_date": parse_xero_date(
            payload.get("Date") or payload.get("DateString")
        ),
        "sub_total": float(payload["SubTotal"]) if payload.get("SubTotal") is not None else None,
        "total_tax": float(payload["TotalTax"]) if payload.get("TotalTax") is not None else None,
        "total": float(payload["Total"]) if payload.get("Total") is not None else None,
        "is_reconciled": (
            bool(payload["IsReconciled"]) if payload.get("IsReconciled") is not None else None
        ),
        "dashboard_business": dashboard_business,
        "xero_updated_at": parse_xero_datetime(payload.get("UpdatedDateUTC")),
        "synced_at": now,
    }
    if row is None:
        row = XeroBankTransaction(
            tenant_id=tenant_id,
            bank_transaction_id=bank_transaction_id,
            **values,
        )
        db.add(row)
        db.flush()
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.lines.clear()
        db.flush()

    for index, line in enumerate(payload.get("LineItems") or []):
        db.add(
            XeroBankTransactionLine(
                bank_transaction_pk=row.id,
                tenant_id=tenant_id,
                bank_transaction_id=bank_transaction_id,
                line_index=index,
                line_item_id=(str(line.get("LineItemID") or "").strip() or None),
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


def sync_bank_transactions_for_organisation(
    db: Session,
    client: httpx.Client,
    *,
    access_token: str,
    organisation: XeroOrganisation,
) -> dict[str, Any]:
    page = 1
    fetched = 0
    while page <= _MAX_PAGES:
        transactions = fetch_bank_transactions_page(
            client,
            access_token=access_token,
            tenant_id=organisation.tenant_id,
            page=page,
        )
        if not transactions:
            break
        for payload in transactions:
            bank_transaction_id = str(payload.get("BankTransactionID") or "").strip()
            lines = payload.get("LineItems") or []
            if bank_transaction_id and not lines:
                payload = (
                    fetch_bank_transaction(
                        client,
                        access_token=access_token,
                        tenant_id=organisation.tenant_id,
                        bank_transaction_id=bank_transaction_id,
                    )
                    or payload
                )
                time.sleep(0.35)
            upsert_bank_transaction(
                db,
                tenant_id=organisation.tenant_id,
                dashboard_business=organisation.dashboard_business,
                payload=payload,
            )
            fetched += 1
        db.commit()
        if len(transactions) < 100:
            break
        page += 1
    return {
        "tenant_id": organisation.tenant_id,
        "tenant_name": organisation.tenant_name,
        "dashboard_business": organisation.dashboard_business,
        "fetched": fetched,
        "pages": page,
    }


def sync_all_bank_transactions(db: Session) -> dict[str, Any]:
    organisations = _mapped_organisations(db)
    if not organisations:
        raise XeroAuthError(
            "Map at least one Xero organisation to a dashboard business before syncing."
        )
    access_token = resolve_access_token(db)
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=90.0) as client:
        for organisation in organisations:
            results.append(
                sync_bank_transactions_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
    return {
        "organisations": results,
        "fetched_total": sum(int(item["fetched"]) for item in results),
    }


def clear_bank_transactions(db: Session) -> None:
    db.execute(delete(XeroBankTransactionLine))
    db.execute(delete(XeroBankTransaction))
    db.commit()
