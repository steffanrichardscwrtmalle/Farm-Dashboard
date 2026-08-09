"""Home-dashboard production averages (7d / 30d) per farm.

Windows are rolling calendar ranges ending on each farm's latest collection
date that has volume (not UK "today"), so a missing day at the end of the
range does not shrink the lookback. Within a window, averages use only days
that have the relevant metric (same style as Collections summary).
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


def _window_metrics(
    points: list[dict[str, Any]],
    *,
    window_end: dt.date,
    days: int,
) -> dict[str, Any]:
    """Mean of daily trend values over ``days`` calendar days ending ``window_end``."""
    start = window_end - dt.timedelta(days=days - 1)
    in_window: list[dict[str, Any]] = []
    for point in points:
        day = _parse_day(point.get("date"))
        if day is None or day < start or day > window_end:
            continue
        in_window.append(point)

    volumes = [
        float(p["volume_litres"])
        for p in in_window
        if p.get("volume_litres") is not None
    ]
    per_cow = [
        float(p["litres_per_cow"])
        for p in in_window
        if p.get("litres_per_cow") is not None
    ]
    fat = [
        float(p["butterfat_pct"])
        for p in in_window
        if p.get("butterfat_pct") is not None
    ]
    protein = [
        float(p["protein_pct"])
        for p in in_window
        if p.get("protein_pct") is not None
    ]
    milk_day = round(sum(volumes) / len(volumes)) if volumes else None
    return {
        "days": days,
        "from": start.isoformat(),
        "to": window_end.isoformat(),
        "days_with_volume": len(volumes),
        "milk_per_cow": _mean(per_cow, 1),
        "milk_per_day": milk_day,
        "butterfat_pct": _mean(fat, 2),
        "protein_pct": _mean(protein, 2),
    }


def _empty_window(days: int, window_end: dt.date | None) -> dict[str, Any]:
    end = window_end.isoformat() if window_end else None
    start = (
        (window_end - dt.timedelta(days=days - 1)).isoformat() if window_end else None
    )
    return {
        "days": days,
        "from": start,
        "to": end,
        "days_with_volume": 0,
        "milk_per_cow": None,
        "milk_per_day": None,
        "butterfat_pct": None,
        "protein_pct": None,
    }


def _latest_volume_date(points: list[dict[str, Any]]) -> dt.date | None:
    latest: dt.date | None = None
    for point in points:
        if point.get("volume_litres") is None:
            continue
        day = _parse_day(point.get("date"))
        if day is not None and (latest is None or day > latest):
            latest = day
    return latest


def get_production_summary(
    db: Session,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Return per-farm 7d/30d production averages for CM and GAD.

    Window end (per farm): latest collection date with volume for that farm.
    Metrics are means of daily trend points (volume summed per day; fat/protein
    and litres/cow already daily means from Collections matching).
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
        window_end = _latest_volume_date(points)
        if window_end is None:
            farms_out.append(
                {
                    "farm": farm,
                    "window_end": None,
                    "d7": _empty_window(7, None),
                    "d30": _empty_window(30, None),
                }
            )
            continue
        farms_out.append(
            {
                "farm": farm,
                "window_end": window_end.isoformat(),
                "d7": _window_metrics(points, window_end=window_end, days=7),
                "d30": _window_metrics(points, window_end=window_end, days=30),
            }
        )

    return {
        "as_of": today.isoformat(),
        "href": "/milk-quality/collections",
        "farms": farms_out,
    }
