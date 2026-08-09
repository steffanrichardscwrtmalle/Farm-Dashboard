"""Import milk haulier collection reports (emailed XLSX) into the database.

Sources, in priority order:
* LOCAL_HAULIER_DIR - a local folder of XLSX files (development; skips Graph mail).
* Microsoft Graph mail - the configured mailbox(es), reading XLSX attachments
  sent by HAULIER_SENDER within the lookback window.

Rows are keyed by (farm, collection_date, sample_id) so re-importing the same
report (the haulier resends a running monthly sheet near-daily) updates rather
than duplicates. After upsert, stale keys within each imported month are pruned
(so re-dated loads do not linger on the old day), then a dedupe pass keeps only
the most recent email's rows for that month.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
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
    HAULIER_LOOKBACK_DAYS,
    HAULIER_MAILBOX_CM,
    HAULIER_MAILBOX_GAD,
    HAULIER_SENDER_DOMAIN,
    LOCAL_HAULIER_DIR,
    graph_cm_is_configured,
)
from app.models import MilkCollection
from app.services.graph_mail import iter_attachments
from app.services.graph_onedrive import get_access_token_for, graph_is_configured
from app.services.haulier_collections import is_editable_collection_source

_FIELDS = (
    "farm",
    "driver",
    "vehicle_reg",
    "arrival_time",
    "depart_time",
    "volume_litres",
    "temp_c",
    "temp_raw",
)

_XLSX_EXT = (".xlsx",)
_XLSX_CTYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# Oldest date used when importing full mailbox history (effectively "everything").
_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def haulier_is_configured() -> bool:
    return bool(LOCAL_HAULIER_DIR) or graph_is_configured()


def _parse_received(value: Any) -> dt.datetime | None:
    """Parse a Graph ISO 'receivedDateTime' into a naive-UTC datetime."""
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
        if status == 400:
            return (
                f"{farm} ({mailbox}): Graph rejected the mail query (400). "
                f"{exc.response.text[:200]}"
            )
        return f"{farm} ({mailbox}): Graph mail request failed ({status})."
    if isinstance(exc, FileNotFoundError):
        return f"{farm} ({mailbox}): {exc}"
    return f"{farm} ({mailbox}): {type(exc).__name__}: {exc}"


def _iter_sources(
    warnings: list[str], since: dt.datetime
) -> Iterator[dict[str, Any]]:
    """Yield {content, source_file, message_id, mailbox_farm} for each XLSX."""
    if LOCAL_HAULIER_DIR:
        folder = Path(LOCAL_HAULIER_DIR)
        if not folder.is_dir():
            raise FileNotFoundError(f"LOCAL_HAULIER_DIR not found: {folder}")
        for path in sorted(folder.rglob("*.xlsx")):
            if path.name.startswith("~$"):
                continue
            yield {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
                "mailbox_farm": "CM",
                "received": dt.datetime.fromtimestamp(path.stat().st_mtime),
            }
        return

    cm_token = None
    if graph_cm_is_configured():
        cm_token = get_access_token_for(
            GRAPH_TENANT_ID_CM, GRAPH_CLIENT_ID_CM, GRAPH_CLIENT_SECRET_CM
        )

    mailboxes = [
        (HAULIER_MAILBOX_CM, "CM", cm_token),
        (HAULIER_MAILBOX_GAD, "GAD", None),
    ]
    for mailbox, farm, token in mailboxes:
        if not mailbox:
            continue
        try:
            for attachment in iter_attachments(
                mailbox,
                sender_domain=HAULIER_SENDER_DOMAIN,
                since=since,
                extensions=_XLSX_EXT,
                content_types=_XLSX_CTYPES,
                token=token,
            ):
                yield {
                    "content": attachment["content"],
                    "source_file": attachment["filename"],
                    "message_id": attachment["message_id"],
                    "mailbox_farm": farm,
                    "received": attachment.get("received"),
                }
        except Exception as exc:  # noqa: BLE001 - one mailbox must not abort the other
            warnings.append(_mailbox_error_message(farm, mailbox, exc))


def import_haulier_collections(
    db: Session, *, full_history: bool = False, days: int | None = None
) -> dict[str, Any]:
    """Read haulier XLSX reports from mail/local folder and upsert collections.

    ``days`` scans the last N days of mail (overrides the default lookback).
    When ``full_history`` is True, every matching email is scanned regardless of
    age; otherwise only the last ``HAULIER_LOOKBACK_DAYS`` days are checked.
    """
    if not haulier_is_configured():
        raise ValueError(
            "Haulier import is not configured. Set Graph API variables or LOCAL_HAULIER_DIR."
        )

    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since = _EPOCH
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=HAULIER_LOOKBACK_DAYS
        )

    # Import lazily so a missing optional dep doesn't break app import.
    from app.services.haulier_xlsx import parse_haulier_xlsx

    parsed_by_key: dict[tuple, dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []

    for source in _iter_sources(warnings, since):
        try:
            result = parse_haulier_xlsx(source["content"])
        except Exception:  # noqa: BLE001 - a single bad file must not abort the run
            files_skipped += 1
            continue

        rows = result.get("rows") or []
        if not rows:
            files_skipped += 1
            continue

        files_processed += 1
        received = _parse_received(source.get("received"))
        for row in rows:
            farm = row.get("farm") or source.get("mailbox_farm") or "CM"
            sample_id = (row.get("sample_id") or "").strip()
            collection_date = row.get("collection_date")
            if collection_date is None:
                continue
            # A load must have a sample number or, failing that, a volume so it
            # still counts toward the monthly total (the haulier sometimes leaves
            # the sample blank).
            if not sample_id and row.get("volume_litres") is None:
                continue
            key = _row_key(
                farm,
                collection_date,
                sample_id,
                row.get("arrival_time"),
            )
            record: dict[str, Any] = {
                "farm": farm,
                "collection_date": collection_date,
                "sample_id": sample_id or None,
                "source_message_id": source.get("message_id"),
                "source_file": source.get("source_file"),
                "source_received": received,
            }
            for field in _FIELDS:
                if field == "farm":
                    continue
                record[field] = row.get(field)
            # When the same load appears in several emails, keep the newest one.
            existing = parsed_by_key.get(key)
            if existing is None or _is_newer(received, existing.get("source_received")):
                parsed_by_key[key] = record

    inserted, updated = _upsert(db, parsed_by_key)
    farms = {key[0] for key in parsed_by_key}
    stale_removed = _prune_stale_month_rows(db, parsed_by_key)
    removed = _dedupe_month_emails(db, farms) + stale_removed
    db.commit()

    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_total": inserted + updated,
        "duplicates_removed": removed,
        "warnings": warnings,
        "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _row_key(
    farm: str,
    collection_date: dt.date,
    sample_id: str,
    arrival: dt.time | None,
) -> tuple:
    """Natural key for a load. Falls back to arrival time when no sample.

    Two sample-less loads on the same day always have different arrival times,
    so arrival is a stable identity that survives a volume correction.
    """
    if sample_id:
        return (farm, collection_date, sample_id)
    return (farm, collection_date, "", arrival)


def _is_newer(candidate: dt.datetime | None, current: dt.datetime | None) -> bool:
    """True if ``candidate`` is a more recent email than ``current``."""
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate >= current


def _email_id(row: MilkCollection) -> str:
    """Stable identity for the email/report a row came from."""
    return row.source_message_id or row.source_file or ""


def _email_recency(row: MilkCollection) -> tuple:
    """Recency of the email a row came from (newest compares greatest)."""
    return (
        row.source_received or dt.datetime.min,
        row.imported_at or dt.datetime.min,
    )


def _prune_stale_month_rows(
    db: Session, parsed_by_key: dict[tuple, dict[str, Any]]
) -> int:
    """Drop haulier rows that are no longer in this import's month snapshot.

    Upsert keys include collection_date, so a re-dated load (e.g. sample 093
    moved from 24 Jul to 25 Jul after a parser fix) inserts the new row but
    leaves the old one. For every (farm, month) present in ``parsed_by_key``,
    delete non-manual rows whose natural key is absent from that snapshot.
    """
    if not parsed_by_key:
        return 0

    months: set[tuple[str, int, int]] = set()
    for farm, collection_date, *_rest in parsed_by_key:
        if collection_date is None:
            continue
        months.add((farm, collection_date.year, collection_date.month))

    removed = 0
    for farm, year, month in months:
        month_start = dt.date(year, month, 1)
        if month == 12:
            month_end = dt.date(year + 1, 1, 1)
        else:
            month_end = dt.date(year, month + 1, 1)
        rows = db.scalars(
            select(MilkCollection).where(
                MilkCollection.farm == farm,
                MilkCollection.collection_date >= month_start,
                MilkCollection.collection_date < month_end,
            )
        ).all()
        for row in rows:
            if is_editable_collection_source(row.source_file):
                continue
            key = _row_key(
                row.farm or "",
                row.collection_date,
                (row.sample_id or "").strip(),
                row.arrival_time,
            )
            if key not in parsed_by_key:
                db.delete(row)
                removed += 1
    return removed


def _dedupe_month_emails(db: Session, farms: set[str]) -> int:
    """Make each (farm, month) reflect only the most recent email for that month.

    The haulier resends a running monthly sheet, and a later email is the
    authoritative snapshot for that month. For every (farm, month) we keep the
    rows from the most recent email and delete rows that came from any earlier
    email, so re-dated/withdrawn loads from older reports disappear.
    """
    if not farms:
        return 0
    rows = db.scalars(
        select(MilkCollection).where(MilkCollection.farm.in_(farms))
    ).all()

    by_month: dict[tuple[str, int, int], list[MilkCollection]] = defaultdict(list)
    for row in rows:
        if row.collection_date is None:
            continue
        # Manual/seed entries are kept alongside emailed haulier sheets.
        if is_editable_collection_source(row.source_file):
            continue
        month = (row.collection_date.year, row.collection_date.month)
        by_month[(row.farm or "", *month)].append(row)

    removed = 0
    for group in by_month.values():
        emails: dict[str, list[MilkCollection]] = defaultdict(list)
        for row in group:
            emails[_email_id(row)].append(row)
        if len(emails) < 2:
            continue
        winner = max(
            emails,
            key=lambda eid: max(_email_recency(r) for r in emails[eid]),
        )
        for eid, items in emails.items():
            if eid == winner:
                continue
            for row in items:
                db.delete(row)
                removed += 1
    return removed


def _upsert(
    db: Session,
    parsed_by_key: dict[tuple, dict[str, Any]],
) -> tuple[int, int]:
    if not parsed_by_key:
        return (0, 0)

    farms = {key[0] for key in parsed_by_key}
    existing_rows = db.scalars(
        select(MilkCollection).where(MilkCollection.farm.in_(farms))
    ).all()
    existing_by_key = {
        _row_key(
            row.farm,
            row.collection_date,
            (row.sample_id or "").strip(),
            row.arrival_time,
        ): row
        for row in existing_rows
    }

    inserted = 0
    updated = 0
    for key, record in parsed_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            db.add(MilkCollection(**record))
            inserted += 1
            continue
        for field in (*_FIELDS, "source_message_id", "source_file", "source_received"):
            setattr(row, field, record.get(field))
        updated += 1

    return (inserted, updated)
