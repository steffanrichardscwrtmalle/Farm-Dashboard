"""Choose a full Xero history read or a changes-only read.

Xero's If-Modified-Since header filters on UpdatedDateUTC, not the document
date. A payment, edit, or void on an old invoice still comes back. With that
header set, deleted records are included too, so a local row can be marked
deleted and drop out of the reports.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    XeroBankTransaction,
    XeroInvoice,
    XeroManualJournal,
    XeroOrganisation,
)

# Re-read a short window so a sync that overlaps an in-progress edit, or a
# clock difference with Xero, cannot skip a change.
MODIFIED_SINCE_OVERLAP = dt.timedelta(days=2)
# A full pass repairs anything a changes-only read could miss, such as a
# delete Xero did not echo, or a previous sync that hit the page cap.
FULL_RESYNC_AFTER = dt.timedelta(days=21)
_PAGE_SIZE = 100


def _naive(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def modified_since_header(since: dt.datetime | None) -> dict[str, str]:
    if since is None:
        return {}
    return {"If-Modified-Since": _naive(since).strftime("%Y-%m-%dT%H:%M:%S")}


def next_modified_since(
    *,
    watermark: dt.datetime | None,
    stored_count: int,
    latest_synced_at: dt.datetime | None,
    page_cap: int,
    now: dt.datetime,
) -> dt.datetime | None:
    """UTC instant for If-Modified-Since, or None when the next sync must be full.

    A missing watermark still uses the newest local synced_at when the stored
    set is smaller than one capped download. That means the first sync after
    this behaviour ships does not repeat a history we already hold.
    """
    now = _naive(now)
    if watermark is not None:
        stored = _naive(watermark)
        if now - stored >= FULL_RESYNC_AFTER:
            return None
        return stored - MODIFIED_SINCE_OVERLAP
    if stored_count <= 0 or latest_synced_at is None:
        return None
    if stored_count >= page_cap * _PAGE_SIZE:
        return None
    return _naive(latest_synced_at) - MODIFIED_SINCE_OVERLAP


def _count_and_latest(db: Session, model, tenant_id: str, *extra):
    count, latest = db.execute(
        select(func.count(), func.max(model.synced_at)).where(model.tenant_id == tenant_id, *extra)
    ).one()
    return int(count or 0), latest


def invoices_modified_since(
    db: Session,
    organisation: XeroOrganisation,
    *,
    page_cap: int,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    count, latest = _count_and_latest(
        db,
        XeroInvoice,
        organisation.tenant_id,
        XeroInvoice.invoice_type.in_(("ACCREC", "ACCPAY")),
    )
    return next_modified_since(
        watermark=organisation.invoices_synced_at,
        stored_count=count,
        latest_synced_at=latest,
        page_cap=page_cap,
        now=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


def credit_notes_modified_since(
    db: Session,
    organisation: XeroOrganisation,
    *,
    page_cap: int,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    count, latest = _count_and_latest(
        db,
        XeroInvoice,
        organisation.tenant_id,
        XeroInvoice.invoice_type.in_(("ACCRECCREDIT", "ACCPAYCREDIT")),
    )
    return next_modified_since(
        watermark=organisation.credit_notes_synced_at,
        stored_count=count,
        latest_synced_at=latest,
        page_cap=page_cap,
        now=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


def journals_modified_since(
    db: Session,
    organisation: XeroOrganisation,
    *,
    page_cap: int,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    count, latest = _count_and_latest(db, XeroManualJournal, organisation.tenant_id)
    return next_modified_since(
        watermark=organisation.journals_synced_at,
        stored_count=count,
        latest_synced_at=latest,
        page_cap=page_cap,
        now=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


def bank_modified_since(
    db: Session,
    organisation: XeroOrganisation,
    *,
    page_cap: int,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    count, latest = _count_and_latest(db, XeroBankTransaction, organisation.tenant_id)
    return next_modified_since(
        watermark=organisation.bank_synced_at,
        stored_count=count,
        latest_synced_at=latest,
        page_cap=page_cap,
        now=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


def overall_sync_mode(*groups: list[dict]) -> str:
    modes = [str(item.get("mode") or "") for group in groups for item in group]
    if modes and all(mode == "incremental" for mode in modes):
        return "incremental"
    return "full"
