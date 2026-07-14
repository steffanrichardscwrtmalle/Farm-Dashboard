"""Import cattle-sale remittance PDFs from email into the database.

Supports Eurofarm Wales cheque reports and Pathway Farming calf remittances.
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
    CATTLE_SALES_LOOKBACK_DAYS,
    CATTLE_SALES_MAILBOX_CM,
    CATTLE_SALES_MAILBOX_GAD,
    CATTLE_SALES_SENDER_DOMAIN,
    GRAPH_CLIENT_ID_CM,
    GRAPH_CLIENT_SECRET_CM,
    GRAPH_TENANT_ID_CM,
    LOCAL_CATTLE_SALES_DIR,
    graph_cm_is_configured,
)
from app.models import CattleSaleLine
from app.services.cattle_sale_pdf import (
    _extract_text,
    is_acceptable_sale_line,
    parse_cattle_sale_pdf,
)
from app.services.graph_mail import iter_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.pathway_farming_pdf import (
    looks_like_pathway_pdf,
    parse_pathway_farming_pdf,
)

_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "result": None,
}


def cattle_sales_is_configured() -> bool:
    return bool(LOCAL_CATTLE_SALES_DIR) or graph_is_configured()


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


def _sender_domains() -> tuple[str, ...]:
    return tuple(
        part.strip().lstrip("@").lower()
        for part in CATTLE_SALES_SENDER_DOMAIN.split(",")
        if part.strip()
    )


def _parse_sale_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None,
    source_file: str | None,
) -> dict[str, Any]:
    """Dispatch Eurofarm vs Pathway remittances by PDF content."""
    text = _extract_text(content)
    if looks_like_pathway_pdf(text):
        return parse_pathway_farming_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    return parse_cattle_sale_pdf(
        content,
        mailbox_farm=mailbox_farm,
        source_file=source_file,
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
    if LOCAL_CATTLE_SALES_DIR:
        folder = Path(LOCAL_CATTLE_SALES_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_CATTLE_SALES_DIR not found: {folder}")
        for path in sorted(folder.rglob("*.pdf")):
            if path.name.startswith("~$"):
                continue
            farm = None
            name_low = path.name.lower()
            if "gad" in name_low or "green acre" in name_low:
                farm = "GAD"
            elif "cm" in name_low or "cwrt" in name_low or "malle" in name_low:
                farm = "CM"
            yield {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
                "mailbox_farm": farm,
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
            cm_token_error = exc

    mailboxes = [
        (CATTLE_SALES_MAILBOX_GAD, "GAD", None),
        (CATTLE_SALES_MAILBOX_CM, "CM", cm_token),
    ]
    domains = _sender_domains()
    for mailbox, farm, token in mailboxes:
        if not mailbox:
            continue
        if farm == "CM" and cm_token_error is not None:
            warnings.append(_mailbox_error_message(farm, mailbox, cm_token_error))
            continue
        for domain in domains:
            try:
                for attachment in iter_attachments(
                    mailbox,
                    sender_domain=domain,
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


def _sale_values_match(row: CattleSaleLine, record: dict[str, Any]) -> bool:
    """True when stored line already matches the freshly parsed values."""
    if abs((row.cold_weight_kg or 0.0) - float(record["cold_weight_kg"] or 0.0)) >= 0.005:
        return False
    if abs((row.amount_gbp or 0.0) - float(record["amount_gbp"] or 0.0)) >= 0.005:
        return False
    row_reject = row.reject_kg
    new_reject = record.get("reject_kg")
    if row_reject is None and new_reject is None:
        pass
    elif row_reject is None or new_reject is None:
        return False
    elif abs(float(row_reject) - float(new_reject)) >= 0.005:
        return False
    if row.kill_date != record.get("kill_date"):
        return False
    return is_acceptable_sale_line(row.cold_weight_kg, row.reject_kg, row.amount_gbp)


def _ingest_one_pdf(
    content: bytes,
    *,
    source_file: str,
    source_message_id: str | None,
    source_received: dt.datetime | None,
    mailbox_farm: str | None,
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]],
    warnings: list[str],
    skipped_files: list[str],
) -> bool:
    received = _parse_received(source_received)
    try:
        result = _parse_sale_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    except Exception:  # noqa: BLE001
        skipped_files.append(f"Could not read PDF: {source_file}")
        return False

    farm = result.get("farm") or mailbox_farm
    sale_date = result.get("sale_date")
    lines = list(result.get("lines") or [])
    file_warnings = list(result.get("warnings") or [])

    for w in file_warnings:
        warnings.append(f"{source_file}: {w}")

    if not farm:
        skipped_files.append(f"{source_file}: could not determine farm")
        return False
    if not lines:
        skipped_files.append(f"{source_file}: no animal lines found")
        return False

    ingested = False
    for line in lines:
        line_sale_date = line.get("kill_date") or sale_date
        if not line_sale_date:
            warnings.append(
                f"{source_file}: no kill date for {line.get('etag', 'unknown tag')}"
            )
            continue
        key = (farm, line["etag"], line_sale_date)
        kill_date = line.get("kill_date") or line_sale_date
        record: dict[str, Any] = {
            "farm": farm,
            "etag": line["etag"],
            "sale_date": line_sale_date,
            "cold_weight_kg": line["cold_weight_kg"],
            "reject_kg": line.get("reject_kg"),
            "kill_date": kill_date,
            "amount_gbp": line["amount_gbp"],
            "source_message_id": source_message_id,
            "source_file": source_file,
            "source_received": received,
        }
        existing = parsed_by_key.get(key)
        if existing is None or _is_newer(received, existing.get("source_received")):
            parsed_by_key[key] = record
            ingested = True
    return ingested


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


def _upsert(
    db: Session,
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]],
) -> tuple[int, int]:
    if not parsed_by_key:
        return (0, 0)

    farms = {key[0] for key in parsed_by_key}
    etags = {key[1] for key in parsed_by_key}
    existing_rows = db.scalars(
        select(CattleSaleLine).where(
            CattleSaleLine.farm.in_(farms),
            CattleSaleLine.etag.in_(etags),
        )
    ).all()
    existing_by_key = {
        (row.farm, row.etag, row.sale_date): row for row in existing_rows
    }

    inserted = 0
    updated = 0
    for key, record in parsed_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            db.add(CattleSaleLine(**record))
            inserted += 1
            continue
        # Always apply corrected parses (e.g. after PDF-parser fixes). Skip only
        # when the stored line already matches and looks acceptable.
        if _sale_values_match(row, record):
            continue
        for field in (
            "cold_weight_kg",
            "reject_kg",
            "kill_date",
            "amount_gbp",
            "source_message_id",
            "source_file",
            "source_received",
        ):
            setattr(row, field, record.get(field))
        updated += 1

    return (inserted, updated)


def import_cattle_sales(
    db: Session,
    *,
    full_history: bool = False,
    days: int | None = None,
) -> dict[str, Any]:
    if not cattle_sales_is_configured():
        raise ValueError(
            "Cattle sales import is not configured. "
            "Set Graph API variables or LOCAL_CATTLE_SALES_DIR."
        )

    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=CATTLE_SALES_LOOKBACK_DAYS
        )

    # Always re-scan matching emails in the window. Skipping by message id hid
    # parser fixes (foreign tags / QAS=NO) after a first partial import.
    files_processed = 0
    files_skipped = 0
    inserted = 0
    updated = 0
    warnings: list[str] = []
    skipped_files: list[str] = []
    batch: dict[tuple[str, str, dt.date], dict[str, Any]] = {}

    def flush() -> None:
        nonlocal inserted, updated, batch
        if not batch:
            return
        ins, upd = _upsert(db, batch)
        db.commit()
        inserted += ins
        updated += upd
        batch = {}

    for source in _iter_sources(warnings, since, frozenset()):
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
        if len(batch) >= 50:
            flush()

    flush()

    if (
        files_processed == 0
        and files_skipped == 0
        and not warnings
        and not skipped_files
    ):
        warnings.append(
            "No cattle-sale PDFs found in the mailboxes for this date range "
            "(Eurofarm / Pathway Farming). Try a longer Range, or Upload PDFs."
        )

    return _import_result(
        files_processed=files_processed,
        files_skipped=files_skipped,
        inserted=inserted,
        updated=updated,
        warnings=warnings,
        skipped_files=skipped_files,
    )


def upload_cattle_sale_pdfs(
    db: Session, files: list[tuple[str, bytes]]
) -> dict[str, Any]:
    if not files:
        raise ValueError("No PDF files provided")

    now = dt.datetime.now()
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]] = {}
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
        farm = None
        name_low = name.lower()
        if "gad" in name_low or "green acre" in name_low:
            farm = "GAD"
        elif "cm" in name_low or "cwrt" in name_low or "malle" in name_low:
            farm = "CM"
        if _ingest_one_pdf(
            content,
            source_file=name,
            source_message_id="manual-upload",
            source_received=now,
            mailbox_farm=farm,
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


def get_import_status() -> dict[str, Any]:
    with _lock:
        return dict(_import_status)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started(*, days: int | None) -> None:
    if days:
        message = f"Scanning mailbox for cattle sales (last {days} days)…"
    else:
        message = "Scanning mailbox for cattle sales…"
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
    db = db_factory()
    try:
        result = import_cattle_sales(
            db,
            full_history=full_history,
            days=days,
        )
        message = (
            f"Imported {result['rows_total']} line(s) "
            f"({result['rows_inserted']} new, {result['rows_updated']} updated) "
            f"from {result['files_processed']} PDF(s)"
            + (
                f", skipped {result['files_skipped']}."
                if result.get("files_skipped")
                else "."
            )
        )
        _set_import_status(status="complete", message=message, result=result)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Background cattle sales import failed")
        _set_import_status(
            status="error",
            message=f"{type(exc).__name__}: {exc}",
            result=None,
        )
    finally:
        db.close()
