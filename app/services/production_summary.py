"""Home-dashboard production averages (7d / 30d) per farm.

Windows are rolling calendar ranges ending on the most recent day that has
the relevant metric for that farm (not UK "today"). Empty trailing days after
the latest load are ignored. Within a window, averages use only days that
have that metric (same style as Collections summary).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS
from app.services.haulier_collections import list_collections

_UK = ZoneInfo("Europe/London")

# Fetch a little more than 30d so a farm whose latest load is a few days
# behind "today" still has a full 30-day window available.
_LOOKBACK_PAD_DAYS = 45
_SHORT_WINDOW_DAYS = 7
_LONG_WINDOW_DAYS = 30


def _uk_today() -> dt.date:
    return dt.datetime.now(_UK).date()


def _parse_day(value: str | dt.date | None) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _mean(values: list[float], dp: int) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), dp)


def _latest_date_for(points: list[dict[str, Any]], key: str) -> dt.date | None:
    latest: dt.date | None = None
    for point in points:
        if point.get(key) is None:
            continue
        day = _parse_day(point.get("date"))
        if day is not None and (latest is None or day > latest):
            latest = day
    return latest


def _metric_window(
    points: list[dict[str, Any]],
    *,
    key: str,
    days: int,
    dp: int,
    as_int: bool = False,
) -> dict[str, Any]:
    """Mean of ``key`` over ``days`` calendar days ending on latest day with that key."""
    window_end = _latest_date_for(points, key)
    if window_end is None:
        return {
            "days": days,
            "from": None,
            "to": None,
            "days_with_data": 0,
            "value": None,
            "window_end": None,
        }
    start = window_end - dt.timedelta(days=days - 1)
    values = [
        float(point[key])
        for point in points
        if point.get(key) is not None
        and (day := _parse_day(point.get("date"))) is not None
        and start <= day <= window_end
    ]
    if as_int:
        value: float | int | None = (
            round(sum(values) / len(values)) if values else None
        )
    else:
        value = _mean(values, dp)
    return {
        "days": days,
        "from": start.isoformat(),
        "to": window_end.isoformat(),
        "days_with_data": len(values),
        "value": value,
        "window_end": window_end.isoformat(),
    }


def _empty_bundle(days: int) -> dict[str, Any]:
    return {
        "days": days,
        "from": None,
        "to": None,
        "days_with_volume": 0,
        "window_end": None,
        "milk_per_cow": None,
        "milk_per_day": None,
        "butterfat_pct": None,
        "protein_pct": None,
    }


def _bundle_for_days(points: list[dict[str, Any]], days: int) -> dict[str, Any]:
    volume = _metric_window(
        points, key="volume_litres", days=days, dp=0, as_int=True
    )
    per_cow = _metric_window(points, key="litres_per_cow", days=days, dp=1)
    fat = _metric_window(points, key="butterfat_pct", days=days, dp=2)
    protein = _metric_window(points, key="protein_pct", days=days, dp=2)
    # Prefer volume window bounds for the bundle metadata (milk widgets).
    return {
        "days": days,
        "from": volume["from"],
        "to": volume["to"],
        "days_with_volume": volume["days_with_data"],
        "window_end": volume["window_end"],
        "milk_per_cow": per_cow["value"],
        "milk_per_day": volume["value"],
        "butterfat_pct": fat["value"],
        "protein_pct": protein["value"],
        "windows": {
            "milk_per_day": {
                "from": volume["from"],
                "to": volume["to"],
                "days_with_data": volume["days_with_data"],
            },
            "milk_per_cow": {
                "from": per_cow["from"],
                "to": per_cow["to"],
                "days_with_data": per_cow["days_with_data"],
            },
            "butterfat_pct": {
                "from": fat["from"],
                "to": fat["to"],
                "days_with_data": fat["days_with_data"],
            },
            "protein_pct": {
                "from": protein["from"],
                "to": protein["to"],
                "days_with_data": protein["days_with_data"],
            },
        },
    }


def get_production_summary(
    db: Session,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Return per-farm 7d/30d production averages for CM and GAD.

    Each metric's window ends on that farm's latest day that has the metric,
    then looks back the calendar span (7 or 30 days).
    """
    today = as_of or _uk_today()
    date_from = today - dt.timedelta(days=_LOOKBACK_PAD_DAYS)
    payload = list_collections(
        db,
        farms=list(HERD_FARM_OPTIONS),
        date_from=date_from,
        date_to=today,
    )
    trend = payload.get("trend") or {}

    farms_out: list[dict[str, Any]] = []
    for farm in HERD_FARM_OPTIONS:
        points = list(trend.get(farm) or [])
        d7 = _bundle_for_days(points, _SHORT_WINDOW_DAYS)
        d30 = _bundle_for_days(points, _LONG_WINDOW_DAYS)
        farms_out.append(
            {
                "farm": farm,
                "window_end": d7.get("window_end") or d30.get("window_end"),
                "d7": d7,
                "d30": d30,
            }
        )

    return {
        "as_of": today.isoformat(),
        "href": "/milk-quality/collections",
        "farms": farms_out,
    }
