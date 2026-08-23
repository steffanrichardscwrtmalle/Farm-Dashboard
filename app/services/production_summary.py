"""Home-dashboard production averages (rolling short / 30d) per farm.

Windows end on the most recent day that has the relevant metric for that farm
(not UK "today"). Empty trailing days after the latest load are ignored.
Within a window, averages use only days that have that metric (same style as
Collections summary).

The short "rolling" figure is the mean of the 6-day and 7-day calendar means
(same blend as Collections trend smoothing), which damps CM's alternate
3-load / 4-load collection pattern better than a plain 7-day mean.
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
_SHORT_ROLL_WINDOWS = (6, 7)
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


def _blend_metric_windows(
    points: list[dict[str, Any]],
    *,
    key: str,
    windows: tuple[int, ...],
    dp: int,
    as_int: bool = False,
) -> dict[str, Any]:
    """Mean of several calendar-window means (e.g. 6d and 7d → rolling)."""
    parts = [
        _metric_window(points, key=key, days=days, dp=dp, as_int=as_int)
        for days in windows
    ]
    values = [part["value"] for part in parts if part["value"] is not None]
    # Metadata from the longest window (widest calendar span).
    longest = max(parts, key=lambda part: int(part["days"] or 0))
    if not values:
        value: float | int | None = None
    elif as_int:
        value = round(sum(float(v) for v in values) / len(values))
    else:
        value = _mean([float(v) for v in values], dp)
    return {
        "days": "rolling",
        "windows": list(windows),
        "from": longest["from"],
        "to": longest["to"],
        "days_with_data": longest["days_with_data"],
        "value": value,
        "window_end": longest["window_end"],
    }


def _empty_bundle(days: int | str) -> dict[str, Any]:
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
        "bactoscan": None,
        "scc": None,
        "milk_temp": None,
    }


def _bundle_from_metric_fn(
    points: list[dict[str, Any]],
    metric_fn,
    *,
    days_label: int | str,
) -> dict[str, Any]:
    volume = metric_fn(points, key="volume_litres", dp=0, as_int=True)
    per_cow = metric_fn(points, key="litres_per_cow", dp=1)
    fat = metric_fn(points, key="butterfat_pct", dp=2)
    protein = metric_fn(points, key="protein_pct", dp=2)
    bactoscan = metric_fn(points, key="bactoscan", dp=0, as_int=True)
    scc = metric_fn(points, key="scc", dp=0, as_int=True)
    temp = metric_fn(points, key="temp_c", dp=1)
    return {
        "days": days_label,
        "from": volume["from"],
        "to": volume["to"],
        "days_with_volume": volume["days_with_data"],
        "window_end": volume["window_end"],
        "milk_per_cow": per_cow["value"],
        "milk_per_day": volume["value"],
        "butterfat_pct": fat["value"],
        "protein_pct": protein["value"],
        "bactoscan": bactoscan["value"],
        "scc": scc["value"],
        "milk_temp": temp["value"],
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
            "bactoscan": {
                "from": bactoscan["from"],
                "to": bactoscan["to"],
                "days_with_data": bactoscan["days_with_data"],
            },
            "scc": {
                "from": scc["from"],
                "to": scc["to"],
                "days_with_data": scc["days_with_data"],
            },
            "milk_temp": {
                "from": temp["from"],
                "to": temp["to"],
                "days_with_data": temp["days_with_data"],
            },
        },
    }


def _bundle_for_days(points: list[dict[str, Any]], days: int) -> dict[str, Any]:
    def metric_fn(pts, *, key, dp, as_int=False):
        return _metric_window(pts, key=key, days=days, dp=dp, as_int=as_int)

    return _bundle_from_metric_fn(points, metric_fn, days_label=days)


def _bundle_for_rolling(points: list[dict[str, Any]]) -> dict[str, Any]:
    def metric_fn(pts, *, key, dp, as_int=False):
        return _blend_metric_windows(
            pts,
            key=key,
            windows=_SHORT_ROLL_WINDOWS,
            dp=dp,
            as_int=as_int,
        )

    return _bundle_from_metric_fn(points, metric_fn, days_label="rolling")


def get_production_summary(
    db: Session,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Return per-farm rolling-short / 30d production averages for CM and GAD.

    Short window (``d7`` key kept for the home widget) is the mean of the
    6-day and 7-day calendar means. Each metric's windows end on that farm's
    latest day that has the metric.
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
        d7 = _bundle_for_rolling(points)
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
