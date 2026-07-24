"""Import parlour milk-flow reports from email (Dataflow / DelPro exports).

Sources, in priority order:
* LOCAL_PARLOUR_DIR — local folder of XLS/CSV files (development; skips Graph mail).
* Microsoft Graph mail — configured mailbox(es), reading attachments from
  PARLOUR_SENDER (default support@dataflow2.com) whose filenames look like
  ``Milk Flow Report Export CM`` / ``… GAD``.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
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
from app.services.graph_mail import iter_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.parlour_milk_flow_import import import_milk_flow_bytes
from app.services.parlour_milk_flow_parse import (
    detect_farm_from_filename,
    is_milk_flow_report_filename,
)

_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
_EXTENSIONS = (".xls", ".xlsx", ".csv")

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


def _iter_sources(
    warnings: list[str],
    since: dt.datetime,
) -> Iterator[dict[str, Any]]:
    if LOCAL_PARLOUR_DIR:
        folder = Path(LOCAL_PARLOUR_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_PARLOUR_DIR not found: {folder}")
        local_items: list[dict[str, Any]] = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name.startswith("~$"):
                continue
            if path.suffix.lower() not in _EXTENSIONS:
                continue
            if not is_milk_flow_report_filename(path.name):
                continue
            local_items.append(
                {
                    "content": path.read_bytes(),
                    "source_file": path.name,
                    "message_id": None,
                    "mailbox_farm": detect_farm_from_filename(path.name),
                    "received": dt.datetime.fromtimestamp(path.stat().st_mtime),
                }
            )
        local_items.sort(
            key=lambda item: item.get("received") or dt.datetime.min,
            reverse=True,
        )
        yield from local_items
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

    collected: list[dict[str, Any]] = []
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
                if not is_milk_flow_report_filename(filename):
                    continue
                collected.append(
                    {
                        "content": attachment["content"],
                        "source_file": filename,
                        "message_id": attachment.get("message_id"),
                        "mailbox_farm": farm,
                        "received": attachment.get("received"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(_mailbox_error_message(farm, mailbox, exc))

    def _received_key(item: dict[str, Any]) -> dt.datetime:
        value = item.get("received")
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=None)
        if not value:
            return dt.datetime.min
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return dt.datetime.min
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed

    # Newest emails first so older cumulative exports cannot clobber fresher data
    # before source_received guards are written.
    collected.sort(key=_received_key, reverse=True)
    yield from collected


def import_parlour_milk_flow(
    db: Session,
    *,
    full_history: bool = False,
    days: int | None = None,
) -> dict[str, Any]:
    if not parlour_is_configured():
        raise ValueError(
            "Parlour import is not configured. "
            "Set Graph API variables or LOCAL_PARLOUR_DIR."
        )

    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=PARLOUR_LOOKBACK_DAYS
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
        farm = detect_farm_from_filename(filename) or source.get("mailbox_farm")
        try:
            batch_results = import_milk_flow_bytes(
                db,
                source["content"],
                filename=filename,
                farm=farm,
                source_message_id=source.get("message_id"),
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
        results.extend(batch_results)
        with _lock:
            if _import_status.get("status") == "running":
                _import_status["message"] = (
                    f"Imported {files_processed} file(s), "
                    f"{shifts_imported} shift(s)…"
                )

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


def mark_import_started(*, days: int | None) -> None:
    if days:
        message = f"Scanning mailbox for milk-flow reports (last {days} days)…"
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
) -> None:
    db = db_factory()
    try:
        result = import_parlour_milk_flow(
            db,
            full_history=full_history,
            days=days,
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
