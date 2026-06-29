"""Import NML milk-quality results from emailed PDF reports into the database.

Sources, in priority order:
* LOCAL_NML_DIR - a local folder of PDFs (development; skips Graph mail).
* Microsoft Graph mail - the configured per-farm mailboxes, reading PDF
  attachments sent by NML_SENDER within the lookback window.

Rows are keyed by (producer_ref, sample_date, sample_id) so re-importing the
same report (or an overlapping lookback window) updates rather than duplicates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import (
    GRAPH_CLIENT_ID_CM,
    GRAPH_CLIENT_SECRET_CM,
    GRAPH_TENANT_ID_CM,
    LOCAL_NML_DIR,
    NML_LOOKBACK_DAYS,
    NML_MAILBOX_CM,
    NML_MAILBOX_GAD,
    NML_SENDER,
    graph_cm_is_configured,
)
from app.models import NmlMilkResult
from app.services.graph_mail import iter_pdf_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.nml_pdf import farm_for_producer_ref, parse_nml_pdf

_SAMPLE_FIELDS = (
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "antibiotic_pass",
    "urea_pct",
)
_META_FIELDS = ("farm", "milk_buyer", "report_month", "report_date")


def nml_is_configured() -> bool:
    return bool(LOCAL_NML_DIR) or graph_is_configured()


def _mailbox_error_message(farm: str, mailbox: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            return (
                f"{farm} ({mailbox}): access denied (403). Grant Mail.Read application "
                "permission and admin consent on the Entra app for this tenant."
            )
        if status == 401:
            return (
                f"{farm} ({mailbox}): authentication failed (401). "
                "Check GRAPH_CLIENT_SECRET for this tenant."
            )
        if status == 400:
            return (
                f"{farm} ({mailbox}): Graph rejected the mail query (400). "
                f"{exc.response.text[:200]}"
            )
        return f"{farm} ({mailbox}): Graph mail request failed ({status})."
    if isinstance(exc, FileNotFoundError):
        return f"{farm} ({mailbox}): {exc}"
    return f"{farm} ({mailbox}): {type(exc).__name__}: {exc}"


# Oldest date used when importing the full mailbox history (effectively "everything").
_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def _iter_sources(
    warnings: list[str], since: dt.datetime
) -> Iterator[dict[str, Any]]:
    """Yield {content, source_file, message_id, mailbox_farm} for each PDF."""
    if LOCAL_NML_DIR:
        folder = Path(LOCAL_NML_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_NML_DIR not found: {folder}")
        for path in sorted(folder.rglob("*.pdf")):
            if path.name.startswith("~$"):
                continue
            yield {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
                "mailbox_farm": None,
            }
        return

    # The Cwrt Malle mailbox lives in its own tenant; use that app's token when
    # configured, otherwise fall back to the default app (same-tenant setups).
    cm_token = None
    if graph_cm_is_configured():
        cm_token = get_access_token_for(
            GRAPH_TENANT_ID_CM, GRAPH_CLIENT_ID_CM, GRAPH_CLIENT_SECRET_CM
        )

    mailboxes = [
        (NML_MAILBOX_GAD, "GAD", None),
        (NML_MAILBOX_CM, "CM", cm_token),
    ]
    for mailbox, farm, token in mailboxes:
        if not mailbox:
            continue
        try:
            for attachment in iter_pdf_attachments(
                mailbox, sender=NML_SENDER, since=since, token=token
            ):
                yield {
                    "content": attachment["content"],
                    "source_file": attachment["filename"],
                    "message_id": attachment["message_id"],
                    "mailbox_farm": farm,
                }
        except Exception as exc:  # noqa: BLE001 - one mailbox must not abort the other
            warnings.append(_mailbox_error_message(farm, mailbox, exc))


def import_nml_results(
    db: Session, *, full_history: bool = False
) -> dict[str, Any]:
    """Read NML PDFs from mail/local folder and upsert milk-quality results.

    When ``full_history`` is True, every matching email is scanned regardless of
    age; otherwise only the last ``NML_LOOKBACK_DAYS`` days are checked.
    """
    if not nml_is_configured():
        raise ValueError(
            "NML import is not configured. Set Graph API variables or LOCAL_NML_DIR."
        )

    if full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NML_LOOKBACK_DAYS)

    # Deduplicate within the run; later reports for the same key win.
    parsed_by_key: dict[tuple[str, dt.date, str], dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []

    for source in _iter_sources(warnings, since):
        try:
            result = parse_nml_pdf(source["content"])
        except Exception:  # noqa: BLE001 - a single bad PDF must not abort the run
            files_skipped += 1
            continue

        metadata = result["metadata"]
        producer_ref = (metadata.get("producer_ref") or "").strip()
        if not producer_ref or not result["samples"]:
            files_skipped += 1
            continue

        files_processed += 1
        farm = metadata.get("farm") or source.get("mailbox_farm")
        for sample in result["samples"]:
            key = (producer_ref, sample["sample_date"], sample["sample_id"])
            record: dict[str, Any] = {
                "producer_ref": producer_ref,
                "sample_date": sample["sample_date"],
                "sample_id": sample["sample_id"],
                "farm": farm,
                "milk_buyer": metadata.get("milk_buyer"),
                "report_month": metadata.get("report_month"),
                "report_date": metadata.get("report_date"),
                "source_message_id": source.get("message_id"),
                "source_file": source.get("source_file"),
            }
            for field in _SAMPLE_FIELDS:
                record[field] = sample.get(field)
            parsed_by_key[key] = record

    inserted, updated = _upsert(db, parsed_by_key)
    db.commit()

    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_total": inserted + updated,
        "warnings": warnings,
        "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _upsert(
    db: Session,
    parsed_by_key: dict[tuple[str, dt.date, str], dict[str, Any]],
) -> tuple[int, int]:
    if not parsed_by_key:
        return (0, 0)

    producer_refs = {key[0] for key in parsed_by_key}
    existing_rows = db.scalars(
        select(NmlMilkResult).where(NmlMilkResult.producer_ref.in_(producer_refs))
    ).all()
    existing_by_key = {
        (row.producer_ref, row.sample_date, row.sample_id): row
        for row in existing_rows
    }

    inserted = 0
    updated = 0
    for key, record in parsed_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            db.add(NmlMilkResult(**record))
            inserted += 1
            continue
        for field in (*_SAMPLE_FIELDS, *_META_FIELDS, "source_message_id", "source_file"):
            setattr(row, field, record.get(field))
        updated += 1

    return (inserted, updated)
