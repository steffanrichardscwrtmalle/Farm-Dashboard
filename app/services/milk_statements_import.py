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
    STATEMENTS_EXTRA_SENDERS,
    STATEMENTS_FRESHWAYS_DOMAIN,
    STATEMENTS_LOOKBACK_DAYS,
    STATEMENTS_MAILBOX_CM,
    STATEMENTS_MAILBOX_GAD,
    graph_cm_is_configured,
)
from app.models import MilkStatement
from app.services.graph_mail import iter_attachments
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
        return f"{farm} ({mailbox}): Graph mail request failed ({status})."
    if isinstance(exc, FileNotFoundError):
        return f"{farm} ({mailbox}): {exc}"
    return f"{farm} ({mailbox}): {type(exc).__name__}: {exc}"


def _iter_sources(
    warnings: list[str], since: dt.datetime
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
    if graph_cm_is_configured():
        cm_token = get_access_token_for(
            GRAPH_TENANT_ID_CM, GRAPH_CLIENT_ID_CM, GRAPH_CLIENT_SECRET_CM
        )

    mailboxes = [
        (STATEMENTS_MAILBOX_GAD, "GAD", None, STATEMENTS_FRESHWAYS_DOMAIN),
        (STATEMENTS_MAILBOX_CM, "CM", cm_token, STATEMENTS_DAIRYPARTNERS_DOMAIN),
    ]
    for mailbox, farm, token, domain in mailboxes:
        if not mailbox:
            continue
        try:
            for attachment in iter_attachments(
                mailbox,
                sender_domain=domain,
                extra_senders=STATEMENTS_EXTRA_SENDERS,
                since=since,
                extensions=(".pdf",),
                content_types=("application/pdf",),
                token=token,
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

    parsed_by_key: dict[tuple[str, dt.date], dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []

    for source in _iter_sources(warnings, since):
        try:
            result = parse_milk_statement_pdf(
                source["content"], default_haulage=STATEMENTS_DEFAULT_HAULAGE
            )
        except Exception:  # noqa: BLE001
            files_skipped += 1
            warnings.append(f"Failed to parse PDF: {source.get('source_file')}")
            continue

        fields = dict(result.get("fields") or {})
        file_warnings = list(result.get("warnings") or [])
        if not _record_is_complete(fields):
            files_skipped += 1
            label = source.get("source_file") or "unknown"
            for w in file_warnings:
                warnings.append(f"{label}: {w}")
            if not file_warnings:
                warnings.append(f"{label}: incomplete statement data")
            continue

        files_processed += 1
        received = _parse_received(source.get("received"))
        farm = fields.get("farm") or source.get("mailbox_farm")
        month = fields["statement_month"]
        key = (farm, month)
        record: dict[str, Any] = {
            "farm": farm,
            "statement_month": month,
            "source_message_id": source.get("message_id"),
            "source_file": source.get("source_file"),
            "source_received": received,
        }
        for field in _STATEMENT_FIELDS:
            record[field] = fields.get(field)

        existing = parsed_by_key.get(key)
        if existing is None or _is_newer(received, existing.get("source_received")):
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
