"""Import and display Feedlync Loaded Mixes → By Ingredient usage."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import FeedUsageRecord
from app.services.feedlync_api import (
    fetch_loaded_mix_ingredient_usage,
    month_bounds,
    previous_calendar_month,
)
from app.services.feedlync_auth import FeedlyncAuthError

_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "latest_import": None,
    "rows_imported": 0,
    "month": None,
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


def mark_import_started(month: str) -> None:
    _set_status(
        status="running",
        message="Starting Feedlync usage import…",
        rows_imported=0,
        month=month,
        needs_auth=False,
    )


def resolve_usage_month(value: str | None, *, today: dt.date | None = None) -> dt.date:
    if value:
        text = value.strip()
        if len(text) == 7 and text[4] == "-":
            text = text + "-01"
        try:
            parsed = dt.date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid month: {value}") from exc
        return parsed.replace(day=1)
    return previous_calendar_month(today)


def _round_kg(value: float) -> float:
    return round(value, 2)


def _round_money(value: float) -> float:
    return round(value, 2)


def build_usage_report(
    rows: list[dict[str, Any]],
    *,
    period_start: dt.date,
    period_end: dt.date,
    import_timestamp: dt.datetime | None = None,
) -> dict[str, Any]:
    days = (period_end - period_start).days + 1
    ingredients = sorted(
        rows,
        key=lambda row: float(row.get("as_fed_kg") or 0),
        reverse=True,
    )
    total_as_fed = sum(float(row.get("as_fed_kg") or 0) for row in ingredients)
    total_dm = sum(float(row.get("dm_kg") or 0) for row in ingredients)
    total_cost = sum(float(row.get("cost") or 0) for row in ingredients)
    return {
        "month": period_start.strftime("%Y-%m"),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "days": days,
        "source": "Loaded Mixes → By Ingredient",
        "ingredients": [
            {
                "ingredient_name": row.get("ingredient_name") or "",
                "as_fed_kg": _round_kg(float(row.get("as_fed_kg") or 0)),
                "dm_kg": _round_kg(float(row.get("dm_kg") or 0)),
                "cost": _round_money(float(row.get("cost") or 0)),
            }
            for row in ingredients
        ],
        "totals": {
            "as_fed_kg": _round_kg(total_as_fed),
            "dm_kg": _round_kg(total_dm),
            "cost": _round_money(total_cost),
        },
        "averages": {
            "as_fed_kg": _round_kg(total_as_fed / days) if days else 0.0,
            "dm_kg": _round_kg(total_dm / days) if days else 0.0,
            "cost": _round_money(total_cost / days) if days else 0.0,
        },
        "latest_import": import_timestamp.isoformat() if import_timestamp else None,
        "row_count": len(ingredients),
    }


def get_usage_report(db: Session, *, month: dt.date) -> dict[str, Any]:
    period_start, period_end = month_bounds(month.year, month.month)
    records = list(
        db.scalars(
            select(FeedUsageRecord)
            .where(FeedUsageRecord.period_start == period_start)
            .order_by(FeedUsageRecord.as_fed_kg.desc(), FeedUsageRecord.ingredient_name)
        ).all()
    )
    latest_import = None
    if records:
        latest_import = max(
            (record.import_timestamp for record in records if record.import_timestamp),
            default=None,
        )
    return build_usage_report(
        [record.to_dict() for record in records],
        period_start=period_start,
        period_end=period_end,
        import_timestamp=latest_import,
    )


def import_feed_usage(db: Session, *, month: dt.date) -> dict[str, Any]:
    period_start, period_end = month_bounds(month.year, month.month)
    month_key = period_start.strftime("%Y-%m")
    _set_status(
        status="running",
        message=f"Fetching Loaded Mixes for {month_key}…",
        rows_imported=0,
        month=month_key,
        needs_auth=False,
    )
    try:
        rows = fetch_loaded_mix_ingredient_usage(
            db, period_start=period_start, period_end=period_end
        )
        if not rows:
            raise ValueError(
                f"No Loaded Mixes ingredient rows returned from Feedlync for {month_key}."
            )

        import_ts = dt.datetime.now()
        db.execute(
            delete(FeedUsageRecord).where(FeedUsageRecord.period_start == period_start)
        )
        db.flush()

        for row in rows:
            db.add(
                FeedUsageRecord(
                    period_start=period_start,
                    period_end=period_end,
                    ingredient_name=str(row["ingredient_name"]),
                    as_fed_kg=float(row["as_fed_kg"]),
                    dm_kg=float(row["dm_kg"]),
                    cost=float(row["cost"]),
                    import_timestamp=import_ts,
                )
            )

        db.commit()
        latest_import = import_ts.isoformat()
        result = {
            "rows_imported": len(rows),
            "month": month_key,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "latest_import": latest_import,
        }
        _set_status(
            status="complete",
            message=f"Imported {len(rows)} ingredients for {month_key}.",
            latest_import=latest_import,
            rows_imported=len(rows),
            month=month_key,
            needs_auth=False,
        )
        return result
    except FeedlyncAuthError as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), month=month_key, needs_auth=True)
        raise
    except Exception as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), month=month_key, needs_auth=False)
        raise


def run_usage_import_in_background(db_factory, month: dt.date) -> None:
    db = db_factory()
    try:
        import_feed_usage(db, month=month)
    except Exception:
        pass
    finally:
        db.close()
