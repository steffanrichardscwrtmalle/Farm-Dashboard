"""Import parlour reports from email (Dataflow / DelPro exports).

Sources, in priority order:
* LOCAL_PARLOUR_DIR — local folder of XLS/CSV files (development; skips Graph mail).
* Microsoft Graph mail — configured mailbox(es), reading attachments from
  PARLOUR_SENDER (default support@dataflow2.com) whose filenames look like
  ``Milk Flow Report Export CM`` / ``… GAD`` or ``Rotary Entry ID CM`` / ``… GAD``.
"""

from __future__ import annotations

import datetime as dt
import gc
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import (
    GRAPH_CLIENT_ID_CM,
    GRAPH_CLIENT_SECRET_CM,
    GRAPH_TENANT_ID_CM,
    LOCAL_PARLOUR_DIR,
    PARLOUR_LOOKBACK_DAYS,
    PARLOUR_MAILBOX_CM,
    PARLOUR_MAILBOX_GAD,
    PARLOUR_SENDER,
    graph_cm_is_configured,
)
from app.models import ParlourMilkFlowImport, ParlourRotaryEntryIdImport
from app.services.graph_mail import iter_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.parlour_milk_flow_import import import_milk_flow_bytes
from app.services.parlour_milk_flow_parse import (
    detect_farm_from_filename,
    is_milk_flow_report_filename,
)
from app.services.parlour_rotary_entry_import import import_rotary_entry_id_bytes
from app.services.parlour_rotary_entry_parse import is_rotary_entry_id_filename

_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
_EXTENSIONS = (".xls", ".xlsx", ".csv")
# Overlap when scanning since last import so clock skew / near-boundary mail is not missed.
_SINCE_LAST_BUFFER = dt.timedelta(hours=12)

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "result": None,
}


def parlour_is_configured() -> bool:
    return bool(LOCAL_PARLOUR_DIR) or graph_is_configured()


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


def _progress_callback(phase: str, messages: int, files: int) -> None:
    with _lock:
        if _import_status.get("status") != "running":
            return
        _import_status["message"] = (
            f"{phase}: checked {messages} email(s), found {files} attachment(s)…"
        )


def _is_parlour_attachment_filename(filename: str) -> bool:
    return is_milk_flow_report_filename(filename) or is_rotary_entry_id_filename(
        filename
    )


def _iter_sources(
    warnings: list[str],
    since: dt.datetime,
) -> Iterator[dict[str, Any]]:
    """Yield parlour attachments one at a time (do not buffer all file bytes).

    Within each mailbox, Graph returns messages newest-first. Farms do not
    overwrite each other, so streaming per mailbox is safe and keeps peak RAM
    to roughly one attachment + its pandas parse.
    """
    if LOCAL_PARLOUR_DIR:
        folder = Path(LOCAL_PARLOUR_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_PARLOUR_DIR not found: {folder}")
        local_paths: list[Path] = []
        for path in folder.rglob("*"):
            if not path.is_file() or path.name.startswith("~$"):
                continue
            if path.suffix.lower() not in _EXTENSIONS:
                continue
            if not _is_parlour_attachment_filename(path.name):
                continue
            local_paths.append(path)
        local_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in local_paths:
            yield {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
                "mailbox_farm": detect_farm_from_filename(path.name),
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
        (PARLOUR_MAILBOX_GAD, "GAD", None),
        (PARLOUR_MAILBOX_CM, "CM", cm_token),
    ]
    sender = (PARLOUR_SENDER or "").strip().lower()
    if not sender:
        warnings.append("PARLOUR_SENDER is empty — no emails will be scanned.")
        return

    for mailbox, farm, token in mailboxes:
        if not mailbox:
            continue
        if farm == "CM" and cm_token_error is not None:
            warnings.append(_mailbox_error_message(farm, mailbox, cm_token_error))
            continue
        try:
            for attachment in iter_attachments(
                mailbox,
                sender=sender,
                since=since,
                extensions=_EXTENSIONS,
                token=token,
                on_progress=_progress_callback,
            ):
                filename = attachment.get("filename") or ""
                if not _is_parlour_attachment_filename(filename):
                    continue
                # Yield immediately — do not accumulate attachment bytes.
                yield {
                    "content": attachment["content"],
                    "source_file": filename,
                    "message_id": attachment.get("message_id"),
                    "mailbox_farm": farm,
                    "received": attachment.get("received"),
                }
        except Exception as exc:  # noqa: BLE001
            warnings.append(_mailbox_error_message(farm, mailbox, exc))


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def resolve_import_since(
    db: Session,
    *,
    full_history: bool = False,
    days: int | None = None,
    since_last_import: bool = False,
) -> dt.datetime:
    """Return the mailbox scan lower bound (UTC, timezone-aware)."""
    now = dt.datetime.now(dt.timezone.utc)
    if days is not None and days > 0:
        return now - dt.timedelta(days=days)
    if full_history:
        return _EPOCH
    if since_last_import:
        candidates = [
            _as_utc(db.scalar(select(func.max(ParlourMilkFlowImport.source_received)))),
            _as_utc(db.scalar(select(func.max(ParlourMilkFlowImport.imported_at)))),
            _as_utc(
                db.scalar(select(func.max(ParlourRotaryEntryIdImport.source_received)))
            ),
            _as_utc(
                db.scalar(select(func.max(ParlourRotaryEntryIdImport.imported_at)))
            ),
        ]
        latest = max(
            (value for value in candidates if value is not None),
            default=None,
        )
        if latest is not None:
            since = latest - _SINCE_LAST_BUFFER
            # Never look further back than the configured catch-up window.
            floor = now - dt.timedelta(days=PARLOUR_LOOKBACK_DAYS)
            return max(since, floor)
    return now - dt.timedelta(days=PARLOUR_LOOKBACK_DAYS)


def import_parlour_milk_flow(
    db: Session,
    *,
    full_history: bool = False,
    days: int | None = None,
    since_last_import: bool = False,
) -> dict[str, Any]:
    if not parlour_is_configured():
        raise ValueError(
            "Parlour import is not configured. "
            "Set Graph API variables or LOCAL_PARLOUR_DIR."
        )

    since = resolve_import_since(
        db,
        full_history=full_history,
        days=days,
        since_last_import=since_last_import,
    )

    files_processed = 0
    files_skipped = 0
    shifts_imported = 0
    rows_imported = 0
    warnings: list[str] = []
    skipped_files: list[str] = []
    results: list[dict[str, Any]] = []

    for source in _iter_sources(warnings, since):
        filename = source.get("source_file") or "unknown"
        message_id = source.get("message_id")
        content = source.pop("content", None)
        try:
            # Re-parse milk-flow attachments in the lookback window so CM Morning
            # date corrections can apply once Day/Night peers exist. Upsert still
            # skips older emails for the same farm/date/shift.
            farm = detect_farm_from_filename(filename) or source.get("mailbox_farm")
            try:
                if is_rotary_entry_id_filename(filename):
                    rotary_result = import_rotary_entry_id_bytes(
                        db,
                        content or b"",
                        filename=filename,
                        farm=farm,
                        source_message_id=message_id,
                        source_received=source.get("received"),
                    )
                    batch_results = [rotary_result]
                else:
                    batch_results = import_milk_flow_bytes(
                        db,
                        content or b"",
                        filename=filename,
                        farm=farm,
                        source_message_id=message_id,
                        source_received=source.get("received"),
                        force=False,
                    )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                files_skipped += 1
                # UnicodeDecodeError subclasses ValueError — treat binary/.xls failures clearly.
                if isinstance(exc, UnicodeDecodeError) or (
                    "utf-8" in str(exc).lower() and "codec" in str(exc).lower()
                ):
                    skipped_files.append(
                        f"{filename}: looks like Excel .xls named .csv — "
                        "needs xlrd (pip install xlrd / redeploy). "
                        f"Detail: {exc}"
                    )
                else:
                    skipped_files.append(f"{filename}: {type(exc).__name__}: {exc}")
                logger.exception("Failed to import parlour attachment %s", filename)
                continue

            applied = [r for r in batch_results if not r.get("skipped")]
            if not applied:
                files_skipped += 1
                skipped_files.append(f"{filename}: skipped (older than existing import)")
                continue

            files_processed += 1
            shifts_imported += len(applied)
            rows_imported += sum(r.get("rows_imported", 0) for r in applied)
            # Keep slim summaries only — full batch payloads retain row lists.
            for item in applied:
                results.append(
                    {
                        "farm": item.get("farm"),
                        "milking_date": item.get("milking_date"),
                        "shift": item.get("shift"),
                        "rows_imported": item.get("rows_imported"),
                        "source_filename": item.get("source_filename") or filename,
                    }
                )
            with _lock:
                if _import_status.get("status") == "running":
                    _import_status["message"] = (
                        f"Imported {files_processed} file(s), "
                        f"{shifts_imported} shift(s)…"
                    )
        finally:
            # Release attachment bytes + ORM identity map between files so the
            # web process (often 512MB) does not accumulate peak across a scan.
            content = None
            source.clear()
            db.expire_all()
            gc.collect()

    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "shifts_imported": shifts_imported,
        "rows_imported": rows_imported,
        "rows_total": rows_imported,
        "rows_inserted": rows_imported,
        "rows_updated": 0,
        "warnings": warnings,
        "skipped_files": skipped_files,
        "results": results,
        "sender": PARLOUR_SENDER,
        "since": since.isoformat(),
    }


def get_import_status() -> dict[str, Any]:
    with _lock:
        return dict(_import_status)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started(
    *,
    days: int | None = None,
    since_last_import: bool = False,
) -> None:
    if days:
        message = f"Scanning mailbox for milk-flow reports (last {days} days)…"
    elif since_last_import:
        message = "Scanning mailbox for milk-flow reports since last import…"
    else:
        message = "Scanning mailbox for milk-flow reports…"
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
    since_last_import: bool = False,
) -> None:
    db = db_factory()
    try:
        result = import_parlour_milk_flow(
            db,
            full_history=full_history,
            days=days,
            since_last_import=since_last_import,
        )
        message = (
            f"Imported {result['shifts_imported']} shift(s) "
            f"({result['rows_imported']} cow rows) from "
            f"{result['files_processed']} file(s)"
            + (
                f", skipped {result['files_skipped']}."
                if result.get("files_skipped")
                else "."
            )
        )
        _set_import_status(status="complete", message=message, result=result)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Background parlour milk-flow import failed")
        _set_import_status(
            status="error",
            message=f"{type(exc).__name__}: {exc}",
            result=None,
        )
    finally:
        db.close()
