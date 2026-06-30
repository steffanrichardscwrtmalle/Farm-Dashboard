"""Import monthly milk buyer statement PDFs from email into the database.

Sources, in priority order:
* LOCAL_STATEMENTS_DIR - a local folder of PDFs (development; skips Graph mail).
* Microsoft Graph mail - Freshways PDFs from the GAD mailbox, Dairy Partners
  PDFs from the CM mailbox (separate tenant when configured).

Rows are keyed by (farm, statement_month). Re-importing updates the record;
newer emails for the same month win when deduplicating within a run.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
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
    LOCAL_STATEMENTS_DIR,
    STATEMENTS_DAIRYPARTNERS_DOMAIN,
    STATEMENTS_DEFAULT_HAULAGE,
    STATEMENTS_FRESHWAYS_DOMAIN,
    STATEMENTS_LOOKBACK_DAYS,
    STATEMENTS_MAILBOX_CM,
    STATEMENTS_MAILBOX_GAD,
    graph_cm_is_configured,
)
from app.models import MilkStatement
from app.services.graph_mail import iter_statement_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.milk_statement_pdf import parse_milk_statement_pdf

_STATEMENT_FIELDS = (
    "supplier",
    "litres_sold",
    "milk_price_ppl",
    "haulage_ppl",
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "thermoduric",
    "fpd",
)

_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "result": None,
}


def statements_is_configured() -> bool:
    return bool(LOCAL_STATEMENTS_DIR) or graph_is_configured()


def _parse_received(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


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
        detail = ""
        try:
            msg = exc.response.json().get("error", {}).get("message", "")
            if msg:
                detail = f" {msg}"
        except Exception:
            pass
        return f"{farm} ({mailbox}): Graph mail request failed ({status}).{detail}"
    if isinstance(exc, FileNotFoundError):
        return f"{farm} ({mailbox}): {exc}"
    return f"{farm} ({mailbox}): {type(exc).__name__}: {exc}"


def _sender_domains(domain_config: str) -> tuple[str, ...]:
    return tuple(
        part.strip().lstrip("@").lower()
        for part in domain_config.split(",")
        if part.strip()
    )


def _progress_callback(phase: str, messages: int, pdfs: int) -> None:
    with _lock:
        if _import_status.get("status") != "running":
            return
        _import_status["message"] = (
            f"{phase}: checked {messages} email(s), found {pdfs} PDF(s)…"
        )


def _iter_sources(
    warnings: list[str],
    since: dt.datetime,
    skip_message_ids: frozenset[str] = frozenset(),
) -> Iterator[dict[str, Any]]:
    if LOCAL_STATEMENTS_DIR:
        folder = Path(LOCAL_STATEMENTS_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_STATEMENTS_DIR not found: {folder}")
        for path in sorted(folder.rglob("*.pdf")):
            if path.name.startswith("~$"):
                continue
            yield {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
                "mailbox_farm": None,
                "received": dt.datetime.fromtimestamp(path.stat().st_mtime),
            }
        return

    cm_token = None
    cm_token_error: Exception | None = None
    if graph_cm_is_configured():
        try:
            cm_token = get_access_token_for(
                GRAPH_TENANT_ID_CM, GRAPH_CLIENT_ID_CM, GRAPH_CLIENT_SECRET_CM
            )
        except Exception as exc:  # noqa: BLE001
            # Don't let a CM auth failure abort the whole import (GAD still works).
            cm_token_error = exc

    mailboxes = [
        (STATEMENTS_MAILBOX_GAD, "GAD", None, _sender_domains(STATEMENTS_FRESHWAYS_DOMAIN)),
        (
            STATEMENTS_MAILBOX_CM,
            "CM",
            cm_token,
            _sender_domains(STATEMENTS_DAIRYPARTNERS_DOMAIN),
        ),
    ]
    for mailbox, farm, token, domains in mailboxes:
        if not mailbox:
            continue
        if farm == "CM" and cm_token_error is not None:
            warnings.append(_mailbox_error_message(farm, mailbox, cm_token_error))
            continue
        try:
            for attachment in iter_statement_attachments(
                mailbox,
                sender_domains=domains,
                skip_message_ids=skip_message_ids,
                since=since,
                extensions=(".pdf",),
                content_types=("application/pdf",),
                token=token,
                on_progress=_progress_callback,
            ):
                yield {
                    "content": attachment["content"],
                    "source_file": attachment["filename"],
                    "message_id": attachment["message_id"],
                    "mailbox_farm": farm,
                    "received": attachment.get("received"),
                }
        except Exception as exc:  # noqa: BLE001
            warnings.append(_mailbox_error_message(farm, mailbox, exc))


def _is_newer(candidate: dt.datetime | None, current: dt.datetime | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate >= current


def _record_is_complete(fields: dict[str, Any]) -> bool:
    return bool(
        fields.get("farm")
        and fields.get("statement_month")
        and fields.get("litres_sold")
        and fields.get("milk_price_ppl") is not None
    )


def _ingest_one_pdf(
    content: bytes,
    *,
    source_file: str,
    source_message_id: str | None,
    source_received: dt.datetime | None,
    mailbox_farm: str | None,
    parsed_by_key: dict[tuple[str, dt.date], dict[str, Any]],
    warnings: list[str],
    skipped_files: list[str],
) -> bool:
    """Parse one PDF and stage it in ``parsed_by_key``. Returns True on success."""
    try:
        result = parse_milk_statement_pdf(
            content, default_haulage=STATEMENTS_DEFAULT_HAULAGE
        )
    except Exception:  # noqa: BLE001
        skipped_files.append(f"Could not read PDF: {source_file}")
        return False

    fields = dict(result.get("fields") or {})
    file_warnings = list(result.get("warnings") or [])
    supplier = result.get("supplier")
    if not _record_is_complete(fields):
        if supplier is None:
            skipped_files.append(f"{source_file}: not a milk statement")
        else:
            for w in file_warnings:
                warnings.append(f"{source_file}: {w}")
            if not file_warnings:
                warnings.append(f"{source_file}: incomplete statement data")
        return False

    received = _parse_received(source_received)
    farm = fields.get("farm") or mailbox_farm
    month = fields["statement_month"]
    key = (farm, month)
    record: dict[str, Any] = {
        "farm": farm,
        "statement_month": month,
        "source_message_id": source_message_id,
        "source_file": source_file,
        "source_received": received,
    }
    for field in _STATEMENT_FIELDS:
        record[field] = fields.get(field)

    existing = parsed_by_key.get(key)
    if existing is None or _is_newer(received, existing.get("source_received")):
        parsed_by_key[key] = record
    return True


def _import_result(
    *,
    files_processed: int,
    files_skipped: int,
    inserted: int,
    updated: int,
    warnings: list[str],
    skipped_files: list[str],
) -> dict[str, Any]:
    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_total": inserted + updated,
        "warnings": warnings,
        "skipped_files": skipped_files,
        "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def import_milk_statements(
    db: Session, *, full_history: bool = False, days: int | None = None
) -> dict[str, Any]:
    if not statements_is_configured():
        raise ValueError(
            "Milk statements import is not configured. "
            "Set Graph API variables or LOCAL_STATEMENTS_DIR."
        )

    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=STATEMENTS_LOOKBACK_DAYS
        )

    # Skip emails we have already imported so re-runs (and large backfills that
    # span multiple runs) stay cheap and avoid re-parsing PDFs.
    skip_message_ids = frozenset(
        mid
        for mid in db.scalars(
            select(MilkStatement.source_message_id).where(
                MilkStatement.source_message_id.isnot(None)
            )
        ).all()
        if mid and mid != "manual-upload"
    )

    files_processed = 0
    files_skipped = 0
    inserted = 0
    updated = 0
    warnings: list[str] = []
    skipped_files: list[str] = []
    batch: dict[tuple[str, dt.date], dict[str, Any]] = {}

    def flush() -> None:
        nonlocal inserted, updated, batch
        if not batch:
            return
        ins, upd = _upsert(db, batch)
        db.commit()  # Commit per batch so a worker timeout/kill keeps progress.
        inserted += ins
        updated += upd
        batch = {}

    for source in _iter_sources(warnings, since, skip_message_ids):
        if _ingest_one_pdf(
            source["content"],
            source_file=source.get("source_file") or "unknown",
            source_message_id=source.get("message_id"),
            source_received=source.get("received"),
            mailbox_farm=source.get("mailbox_farm"),
            parsed_by_key=batch,
            warnings=warnings,
            skipped_files=skipped_files,
        ):
            files_processed += 1
        else:
            files_skipped += 1
        if len(batch) >= 20:
            flush()

    flush()

    return _import_result(
        files_processed=files_processed,
        files_skipped=files_skipped,
        inserted=inserted,
        updated=updated,
        warnings=warnings,
        skipped_files=skipped_files,
    )


def upload_milk_statement_pdfs(
    db: Session, files: list[tuple[str, bytes]]
) -> dict[str, Any]:
    """Import one or more statement PDFs uploaded through the dashboard."""
    if not files:
        raise ValueError("No PDF files provided")

    now = dt.datetime.now()
    parsed_by_key: dict[tuple[str, dt.date], dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []
    skipped_files: list[str] = []

    for filename, content in files:
        name = (filename or "upload.pdf").strip() or "upload.pdf"
        if not name.lower().endswith(".pdf"):
            warnings.append(f"{name}: not a PDF file")
            files_skipped += 1
            continue
        if not content:
            warnings.append(f"{name}: empty file")
            files_skipped += 1
            continue
        if _ingest_one_pdf(
            content,
            source_file=name,
            source_message_id="manual-upload",
            source_received=now,
            mailbox_farm=None,
            parsed_by_key=parsed_by_key,
            warnings=warnings,
            skipped_files=skipped_files,
        ):
            files_processed += 1
        else:
            files_skipped += 1

    inserted, updated = _upsert(db, parsed_by_key)
    db.commit()

    return _import_result(
        files_processed=files_processed,
        files_skipped=files_skipped,
        inserted=inserted,
        updated=updated,
        warnings=warnings,
        skipped_files=skipped_files,
    )


def _upsert(
    db: Session,
    parsed_by_key: dict[tuple[str, dt.date], dict[str, Any]],
) -> tuple[int, int]:
    if not parsed_by_key:
        return (0, 0)

    farms = {key[0] for key in parsed_by_key}
    existing_rows = db.scalars(
        select(MilkStatement).where(MilkStatement.farm.in_(farms))
    ).all()
    existing_by_key = {
        (row.farm, row.statement_month): row for row in existing_rows
    }

    inserted = 0
    updated = 0
    for key, record in parsed_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            db.add(MilkStatement(**record))
            inserted += 1
            continue
        # Most recent statement for a month wins. Only overwrite when the
        # incoming email is at least as recent as the stored one (records with
        # no received timestamp are treated as authoritative so manual/local
        # imports still apply).
        incoming = record.get("source_received")
        if incoming is not None and not _is_newer(incoming, row.source_received):
            continue
        for field in (*_STATEMENT_FIELDS, "source_message_id", "source_file", "source_received"):
            setattr(row, field, record.get(field))
        updated += 1

    return (inserted, updated)


def get_import_status() -> dict[str, Any]:
    with _lock:
        return dict(_import_status)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started(*, days: int | None) -> None:
    if days:
        message = f"Scanning mailbox for statements (last {days} days)…"
    else:
        message = "Scanning mailbox for statements…"
    with _lock:
        _import_status.update(status="running", message=message, result=None)


def _set_import_status(**kwargs: Any) -> None:
    with _lock:
        _import_status.update(kwargs)


def run_import_in_background(
    db_factory,
    *,
    full_history: bool = False,
    days: int | None = None,
) -> None:
    """Background worker for dashboard imports (avoids Render HTTP timeouts)."""
    db = db_factory()
    try:
        result = import_milk_statements(db, full_history=full_history, days=days)
        message = (
            f"Imported {result['rows_total']} month(s) "
            f"({result['rows_inserted']} new, {result['rows_updated']} updated)."
        )
        _set_import_status(status="complete", message=message, result=result)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Background milk statements import failed")
        _set_import_status(
            status="error",
            message=f"{type(exc).__name__}: {exc}",
            result=None,
        )
    finally:
        db.close()
