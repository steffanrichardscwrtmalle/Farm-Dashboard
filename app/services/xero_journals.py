"""Fetch and store Xero manual journals for Actual Data."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.config import XERO_API_BASE_URL
from app.models import XeroManualJournal, XeroManualJournalLine, XeroOrganisation
from app.services.xero_auth import XeroAuthError
from app.services.xero_dates import parse_xero_date, parse_xero_datetime
from app.services.xero_orgs import resolve_access_token

JOURNAL_STATUSES = frozenset({"POSTED"})
_MAX_PAGES = 50


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
            time.sleep(2 ** attempt)
            continue
        if response.status_code >= 400:
            detail = response.text.strip()
            raise XeroAuthError(
                f"Failed to fetch manual journals for tenant {tenant_id[:8]}… ({detail[:240]})"
            )
        return response.json() or {}
    raise XeroAuthError("Xero rate-limited manual journal sync. Try again shortly.")


def fetch_manual_journals_page(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    page: int,
) -> list[dict[str, Any]]:
    payload = _get_json(
        client,
        f"{XERO_API_BASE_URL}/ManualJournals",
        access_token=access_token,
        tenant_id=tenant_id,
        params={"page": page},
    )
    return list(payload.get("ManualJournals") or [])


def fetch_manual_journal(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
    manual_journal_id: str,
) -> dict[str, Any]:
    payload = _get_json(
        client,
        f"{XERO_API_BASE_URL}/ManualJournals/{manual_journal_id}",
        access_token=access_token,
        tenant_id=tenant_id,
    )
    journals = payload.get("ManualJournals") or []
    return journals[0] if journals else {}


def upsert_manual_journal(
    db: Session,
    *,
    tenant_id: str,
    dashboard_business: str | None,
    payload: dict[str, Any],
) -> None:
    journal_id = str(payload.get("ManualJournalID") or "").strip()
    if not journal_id:
        return

    row = db.scalar(
        select(XeroManualJournal)
        .where(XeroManualJournal.tenant_id == tenant_id)
        .where(XeroManualJournal.manual_journal_id == journal_id)
        .options(selectinload(XeroManualJournal.lines))
    )
    now = _utcnow()
    values = {
        "narration": (str(payload.get("Narration") or "").strip() or None),
        "status": (str(payload.get("Status") or "").strip() or None),
        "journal_date": parse_xero_date(payload.get("Date") or payload.get("DateString")),
        "dashboard_business": dashboard_business,
        "xero_updated_at": parse_xero_datetime(payload.get("UpdatedDateUTC")),
        "synced_at": now,
    }
    if row is None:
        row = XeroManualJournal(tenant_id=tenant_id, manual_journal_id=journal_id, **values)
        db.add(row)
        db.flush()
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.lines.clear()
        db.flush()

    for index, line in enumerate(payload.get("JournalLines") or []):
        db.add(
            XeroManualJournalLine(
                journal_pk=row.id,
                tenant_id=tenant_id,
                manual_journal_id=journal_id,
                line_index=index,
                description=(str(line.get("Description") or "").strip() or None),
                line_amount=(
                    float(line["LineAmount"]) if line.get("LineAmount") is not None else None
                ),
                account_code=(str(line.get("AccountCode") or "").strip() or None),
                account_id=(str(line.get("AccountID") or "").strip() or None),
                tax_type=(str(line.get("TaxType") or "").strip() or None),
            )
        )


def sync_journals_for_organisation(
    db: Session,
    client: httpx.Client,
    *,
    access_token: str,
    organisation: XeroOrganisation,
) -> dict[str, Any]:
    page = 1
    fetched = 0
    while page <= _MAX_PAGES:
        journals = fetch_manual_journals_page(
            client,
            access_token=access_token,
            tenant_id=organisation.tenant_id,
            page=page,
        )
        if not journals:
            break
        for payload in journals:
            journal_id = str(payload.get("ManualJournalID") or "").strip()
            lines = payload.get("JournalLines") or []
            if journal_id and not lines:
                payload = fetch_manual_journal(
                    client,
                    access_token=access_token,
                    tenant_id=organisation.tenant_id,
                    manual_journal_id=journal_id,
                ) or payload
                time.sleep(0.35)
            upsert_manual_journal(
                db,
                tenant_id=organisation.tenant_id,
                dashboard_business=organisation.dashboard_business,
                payload=payload,
            )
            fetched += 1
        db.commit()
        if len(journals) < 100:
            break
        page += 1
    return {
        "tenant_id": organisation.tenant_id,
        "tenant_name": organisation.tenant_name,
        "dashboard_business": organisation.dashboard_business,
        "fetched": fetched,
        "pages": page,
    }


def sync_all_journals(db: Session) -> dict[str, Any]:
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
                sync_journals_for_organisation(
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


def clear_journals(db: Session) -> None:
    db.execute(delete(XeroManualJournalLine))
    db.execute(delete(XeroManualJournal))
    db.commit()
