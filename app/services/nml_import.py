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
from app.services.graph_mail import iter_attachments, iter_pdf_attachments, probe_mailbox
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.nml_pdf import parse_nml_pdf

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

_GAD_GRAPH_VARS = (
    "GRAPH_TENANT_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_CLIENT_SECRET",
    "GRAPH_DRIVE_USER_EMAIL",
)


def nml_is_configured() -> bool:
    return bool(LOCAL_NML_DIR) or graph_is_configured() or graph_cm_is_configured()


def _mailbox_error_message(farm: str, mailbox: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            return (
                f"{farm} ({mailbox}): access denied (403). Grant Mail.Read application "
                "permission and admin consent on the Entra app for this tenant, and "
                "add an Exchange application access policy for this mailbox."
            )
        if status == 401:
            return (
                f"{farm} ({mailbox}): authentication failed (401). "
                "Check GRAPH_CLIENT_SECRET for this tenant."
            )
        detail = ""
        try:
            msg = exc.response.json().get("error", {}).get("message", "")
            if msg:
                detail = f" {msg}"
        except Exception:
            pass
        if status == 400:
            return (
                f"{farm} ({mailbox}): Graph rejected the mail query (400).{detail}"
            )
        return f"{farm} ({mailbox}): Graph mail request failed ({status}).{detail}"
    if isinstance(exc, FileNotFoundError):
        return f"{farm} ({mailbox}): {exc}"
    return f"{farm} ({mailbox}): {type(exc).__name__}: {exc}"


_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def _looks_like_nml_pdf(content: bytes) -> bool:
    try:
        result = parse_nml_pdf(content)
    except Exception:  # noqa: BLE001
        return False
    metadata = result.get("metadata") or {}
    return bool(metadata.get("producer_ref") and result.get("samples"))


def _iter_mailbox_nml_pdfs(
    mailbox: str,
    farm: str,
    token: str | None,
    since: dt.datetime,
    warnings: list[str],
    mailbox_stats: dict[str, dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Yield NML PDFs from one mailbox using the original sender-filtered import."""
    if farm == "GAD" and not graph_is_configured():
        warnings.append(
            "GAD: Green Acre Graph credentials are not configured on this server "
            f"({', '.join(_GAD_GRAPH_VARS)}). CM uses GRAPH_*_CM; GAD needs the "
            "main GRAPH_* app with Mail.Read for "
            f"{mailbox}."
        )
        mailbox_stats[farm] = {
            "mailbox": mailbox,
            "pdfs_found": 0,
            "error": "graph_not_configured",
        }
        return

    if farm == "CM" and token is None and graph_cm_is_configured():
        warnings.append(
            f"CM ({mailbox}): Cwrt Malle Graph credentials failed; skipping CM mailbox."
        )
        mailbox_stats[farm] = {
            "mailbox": mailbox,
            "pdfs_found": 0,
            "error": "cm_graph_auth_failed",
        }
        return

    probe = probe_mailbox(mailbox, token=token)
    if not probe.get("ok"):
        warnings.append(
            f"{farm} ({mailbox}): cannot read mailbox via Graph "
            f"({probe.get('status')}): {probe.get('message', 'unknown error')}"
        )
        mailbox_stats[farm] = {
            "mailbox": mailbox,
            "pdfs_found": 0,
            "error": f"probe_{probe.get('status')}",
        }
        return

    pdfs_found = 0
    seen_message_ids: set[str] = set()

    def yield_unique(attachment: dict) -> Iterator[dict[str, Any]]:
        nonlocal pdfs_found
        message_id = attachment.get("message_id")
        if message_id and message_id in seen_message_ids:
            return
        if message_id:
            seen_message_ids.add(message_id)
        pdfs_found += 1
        yield {
            "content": attachment["content"],
            "source_file": attachment["filename"],
            "message_id": message_id,
            "mailbox_farm": farm,
        }

    # Original import path (36909e0): exact NML sender, server-side Graph filter.
    for attachment in iter_pdf_attachments(
        mailbox, sender=NML_SENDER, since=since, token=token
    ):
        yield from yield_unique(attachment)

    if pdfs_found:
        mailbox_stats[farm] = {
            "mailbox": mailbox,
            "pdfs_found": pdfs_found,
            "scan_method": "sender",
        }
        return

    # Fallback: open every PDF in the date range and keep ones that parse as NML.
    pdf_candidates = 0
    for attachment in iter_attachments(
        mailbox,
        since=since,
        extensions=(".pdf",),
        content_types=("application/pdf",),
        token=token,
    ):
        pdf_candidates += 1
        if not _looks_like_nml_pdf(attachment["content"]):
            continue
        yield from yield_unique(attachment)

    mailbox_stats[farm] = {
        "mailbox": mailbox,
        "pdfs_found": pdfs_found,
        "pdf_candidates": pdf_candidates,
        "scan_method": "content" if pdfs_found else "none",
    }
    if pdfs_found == 0:
        warnings.append(
            f"{farm} ({mailbox}): no NML report PDFs found since "
            f"{since.date().isoformat()} "
            f"({pdf_candidates} other PDF(s) checked)."
        )


def _iter_sources(
    warnings: list[str],
    since: dt.datetime,
    mailbox_stats: dict[str, dict[str, Any]],
) -> Iterator[dict[str, Any]]:
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

    cm_token = None
    if graph_cm_is_configured():
        try:
            cm_token = get_access_token_for(
                GRAPH_TENANT_ID_CM, GRAPH_CLIENT_ID_CM, GRAPH_CLIENT_SECRET_CM
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(_mailbox_error_message("CM", NML_MAILBOX_CM, exc))

    mailboxes = [
        (NML_MAILBOX_GAD, "GAD", None),
        (NML_MAILBOX_CM, "CM", cm_token),
    ]
    for mailbox, farm, token in mailboxes:
        if not mailbox:
            warnings.append(f"{farm}: mailbox not configured (set NML_MAILBOX_{farm}).")
            continue
        try:
            yield from _iter_mailbox_nml_pdfs(
                mailbox, farm, token, since, warnings, mailbox_stats
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(_mailbox_error_message(farm, mailbox, exc))
            mailbox_stats[farm] = {
                "mailbox": mailbox,
                "pdfs_found": 0,
                "error": type(exc).__name__,
            }


def import_nml_results(
    db: Session, *, full_history: bool = False, days: int | None = None
) -> dict[str, Any]:
    if not nml_is_configured():
        raise ValueError(
            "NML import is not configured. Set Graph API variables or LOCAL_NML_DIR."
        )

    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NML_LOOKBACK_DAYS)

    parsed_by_key: dict[tuple[str, dt.date, str], dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []
    mailbox_stats: dict[str, dict[str, Any]] = {}

    for source in _iter_sources(warnings, since, mailbox_stats):
        source_file = source.get("source_file") or "unknown"
        try:
            result = parse_nml_pdf(source["content"])
        except Exception:  # noqa: BLE001
            files_skipped += 1
            warnings.append(f"{source_file}: could not read PDF")
            continue

        metadata = result["metadata"]
        producer_ref = (metadata.get("producer_ref") or "").strip()
        if not producer_ref or not result["samples"]:
            files_skipped += 1
            if not producer_ref:
                warnings.append(f"{source_file}: no producer reference found")
            else:
                warnings.append(f"{source_file}: no sample rows found")
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
                "source_file": source_file,
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
        "mailbox_stats": mailbox_stats,
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
