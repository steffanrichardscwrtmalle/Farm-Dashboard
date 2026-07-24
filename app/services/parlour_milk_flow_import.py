"""Import milk-flow shift reports into the database."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_milk_flow_parse import (
    ParsedMilkFlowReport,
    parse_milk_flow_report,
)

logger = logging.getLogger(__name__)


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


def _upsert_parsed_report(
    db: Session,
    parsed: ParsedMilkFlowReport,
    *,
    source_message_id: str | None = None,
    source_received: dt.datetime | None = None,
    force: bool = False,
) -> dict | None:
    existing = db.scalar(
        select(ParlourMilkFlowImport).where(
            ParlourMilkFlowImport.farm == parsed.farm,
            ParlourMilkFlowImport.milking_date == parsed.milking_date,
            ParlourMilkFlowImport.shift == parsed.shift,
        )
    )
    replaced = False
    if existing is not None:
        # Skip older email attachments so out-of-order mailbox scans cannot
        # overwrite a newer import for the same farm/date/shift.
        if (
            not force
            and source_received is not None
            and existing.source_received is not None
            and source_received < existing.source_received
        ):
            logger.info(
                "Skipping older milk-flow import farm=%s date=%s shift=%s "
                "(incoming %s < existing %s)",
                parsed.farm,
                parsed.milking_date,
                parsed.shift,
                source_received,
                existing.source_received,
            )
            return {
                "ok": True,
                "skipped": True,
                "reason": "older_source",
                "import_id": existing.id,
                "farm": existing.farm,
                "milking_date": existing.milking_date.isoformat(),
                "shift": existing.shift,
                "rows_imported": existing.rows_imported,
                "replaced": False,
                "source_filename": existing.source_filename,
            }

        db.execute(
            delete(ParlourMilkFlowRow).where(
                ParlourMilkFlowRow.import_id == existing.id
            )
        )
        batch = existing
        batch.source_filename = parsed.source_filename
        batch.rows_imported = 0
        replaced = True
    else:
        batch = ParlourMilkFlowImport(
            farm=parsed.farm,
            milking_date=parsed.milking_date,
            shift=parsed.shift,
            source_filename=parsed.source_filename,
            rows_imported=0,
        )
        db.add(batch)
        db.flush()

    if source_message_id is not None:
        batch.source_message_id = source_message_id
    if source_received is not None:
        batch.source_received = source_received

    db_rows = [
        ParlourMilkFlowRow(
            import_id=batch.id,
            farm=parsed.farm,
            milking_date=r.milking_date,
            shift=r.shift,
            cow_id=r.cow_id,
            pen=r.pen,
            milking_point=r.milking_point,
            dim=r.dim,
            yield_kg=r.yield_kg,
            average_flow=r.average_flow,
            peak_flow=r.peak_flow,
            time_to_peak_seconds=r.time_to_peak_seconds,
            flow_15s=r.flow_15s,
            flow_30s=r.flow_30s,
            flow_60s=r.flow_60s,
            flow_120s=r.flow_120s,
            pct_2_minutes=r.pct_2_minutes,
            milk_yield_2_minutes=r.milk_yield_2_minutes,
            flow_rate_at_removal=r.flow_rate_at_removal,
            duration_seconds=r.duration_seconds,
            start_seconds=r.start_seconds,
            identified_at_milking=r.identified_at_milking,
            final_detaching=r.final_detaching,
            extra_attachments=r.extra_attachments,
        )
        for r in parsed.rows
    ]
    db.add_all(db_rows)
    batch.rows_imported = len(db_rows)
    db.flush()

    logger.info(
        "Imported milk flow report farm=%s date=%s shift=%s rows=%s replaced=%s",
        parsed.farm,
        parsed.milking_date,
        parsed.shift,
        len(db_rows),
        replaced,
    )
    return {
        "ok": True,
        "skipped": False,
        "import_id": batch.id,
        "farm": batch.farm,
        "milking_date": batch.milking_date.isoformat(),
        "shift": batch.shift,
        "rows_imported": batch.rows_imported,
        "replaced": replaced,
        "source_filename": batch.source_filename,
    }


def import_milk_flow_bytes(
    db: Session,
    content: bytes,
    *,
    filename: str = "",
    farm: str | None = None,
    source_message_id: str | None = None,
    source_received: dt.datetime | str | None = None,
    force: bool = False,
) -> list[dict]:
    """Import a milk-flow file; splits into one DB batch per date+shift."""
    reports = parse_milk_flow_report(content, filename=filename, farm=farm)
    received = _parse_received(source_received)
    results: list[dict] = []
    for parsed in reports:
        result = _upsert_parsed_report(
            db,
            parsed,
            source_message_id=source_message_id,
            source_received=received,
            force=force,
        )
        if result is not None:
            results.append(result)
    db.commit()
    return results


def upload_milk_flow_files(
    db: Session,
    payloads: list[tuple[str, bytes]],
    *,
    farm: str | None = None,
) -> dict:
    results: list[dict] = []
    errors: list[str] = []
    for filename, content in payloads:
        if not content:
            errors.append(f"{filename}: empty file")
            continue
        try:
            results.extend(
                import_milk_flow_bytes(
                    db,
                    content,
                    filename=filename,
                    farm=farm,
                    force=True,
                )
            )
        except ValueError as exc:
            db.rollback()
            errors.append(f"{filename}: {exc}")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("Milk flow upload failed for %s", filename)
            errors.append(f"{filename}: {exc}")

    return {
        "ok": not errors,
        "imported": len(results),
        "results": results,
        "errors": errors,
    }
