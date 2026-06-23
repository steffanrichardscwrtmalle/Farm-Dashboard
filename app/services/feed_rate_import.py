"""Import feed rate data from Feedlync into the database."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import FeedRateRecord
from app.services.feed_rate_display import build_feed_rate_display
from app.services.feedlync_api import fetch_feed_data
from app.services.feedlync_auth import FeedlyncAuthError

_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "latest_import": None,
    "rows_imported": 0,
    "ration_names": [],
    "needs_auth": False,
}


def get_import_status() -> dict[str, Any]:
    with _lock:
        return dict(_import_status)


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _import_status.update(kwargs)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started() -> None:
    _set_status(status="running", message="Starting Feedlync import…", rows_imported=0, needs_auth=False)


def import_feed_rate(db: Session) -> dict[str, Any]:
    """Fetch from Feedlync API and replace feed_rate_records with the latest snapshot."""
    _set_status(status="running", message="Fetching feed data from Feedlync…", rows_imported=0, needs_auth=False)

    try:
        rows = fetch_feed_data(db)
        if not rows:
            raise ValueError("No feed data rows returned from Feedlync")

        import_ts = dt.datetime.now()
        db.execute(delete(FeedRateRecord))
        db.flush()

        ration_names: list[str] = []
        seen_rations: set[str] = set()
        for row in rows:
            record = FeedRateRecord(
                ration_name=row["ration_name"],
                group_name=row["group_name"],
                cow_count=row.get("cow_count"),
                feed_percent=row.get("feed_percent"),
                total_fresh=row.get("total_fresh"),
                total_dm=row.get("total_dm"),
                dm_kg_per_cow=row.get("dm_kg_per_cow"),
                cost=row.get("cost"),
                scraped_date=row["scraped_date"],
                import_timestamp=import_ts,
            )
            db.add(record)
            if row["ration_name"] not in seen_rations:
                seen_rations.add(row["ration_name"])
                ration_names.append(row["ration_name"])

        db.commit()
        latest_import = import_ts.isoformat()
        result = {
            "rows_imported": len(rows),
            "ration_names": ration_names,
            "latest_import": latest_import,
        }
        _set_status(
            status="complete",
            message=f"Imported {len(rows)} rows from Feedlync.",
            latest_import=latest_import,
            rows_imported=len(rows),
            ration_names=ration_names,
            needs_auth=False,
        )
        return result
    except FeedlyncAuthError as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), needs_auth=True)
        raise
    except Exception as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), needs_auth=False)
        raise


def run_import_in_background(db_factory) -> None:
    """Background task wrapper — opens its own DB session."""
    db = db_factory()
    try:
        import_feed_rate(db)
    except Exception:
        pass
    finally:
        db.close()


def get_feed_rate_report(db: Session, *, ration: str | None = None) -> dict[str, Any]:
    query = select(FeedRateRecord).order_by(
        FeedRateRecord.ration_name,
        FeedRateRecord.group_name,
    )

    records = list(db.scalars(query).all())
    latest_import = db.scalar(select(func.max(FeedRateRecord.import_timestamp)))
    scraped_date = db.scalar(select(func.max(FeedRateRecord.scraped_date)))

    raw_rows = [record.to_dict() for record in records]
    display = build_feed_rate_display(raw_rows)

    return {
        "rows": raw_rows,
        "ration_options": display["ration_names"],
        "ration_names": display["ration_names"],
        "ration_tables": display["ration_tables"],
        "summary_rows": display["summary_rows"],
        "selected_ration": ration,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "scraped_date": scraped_date.isoformat() if scraped_date else None,
        "row_count": len(records),
    }
