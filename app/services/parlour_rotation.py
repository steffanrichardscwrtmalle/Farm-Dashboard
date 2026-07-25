"""Parlour rotation time from successive starts at the same milking point."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import fmean, median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowRow
from app.services.parlour_milk_flow_parse import (
    shift_timeline_origin,
    to_absolute_start,
)

# Plausible rotary cycle bounds (seconds). Outside = empty stall / shift gap / glitch.
MIN_ROTATION_SECONDS = 180  # 3 min
MAX_ROTATION_SECONDS = 1500  # 25 min
DEFAULT_MA_WINDOW = 40
MAX_POINTS = 25000


def rotation_stats_from_point_starts(
    point_start_pairs: list[tuple[Any, Any]],
    *,
    min_seconds: int = MIN_ROTATION_SECONDS,
    max_seconds: int = MAX_ROTATION_SECONDS,
) -> dict[str, Any]:
    """Median rotation from successive starts at each milking point.

    ``point_start_pairs`` are ``(milking_point, start_seconds)`` for cows in one
    shift session. Gaps outside ``min_seconds``–``max_seconds`` are dropped.
    """
    empty = {
        "median_rotation_seconds": None,
        "median_rotation_minutes": None,
        "rotation_gap_n": 0,
    }
    usable = [
        (int(point), int(start))
        for point, start in point_start_pairs
        if point is not None and start is not None
    ]
    if len(usable) < 2:
        return empty

    origin = shift_timeline_origin([start for _, start in usable])
    if origin is None:
        return empty

    by_point: dict[int, list[int]] = defaultdict(list)
    for point, start in usable:
        by_point[point].append(to_absolute_start(start, origin))

    gaps: list[float] = []
    for abs_starts in by_point.values():
        ordered = sorted(abs_starts)
        for prev, nxt in zip(ordered, ordered[1:]):
            gap = nxt - prev
            if min_seconds <= gap <= max_seconds:
                gaps.append(float(gap))

    if not gaps:
        return empty
    med = median(gaps)
    return {
        "median_rotation_seconds": int(round(med)),
        "median_rotation_minutes": round(med / 60.0, 1),
        "rotation_gap_n": len(gaps),
    }


def rotation_date_bounds(
    db: Session,
    *,
    farm: str | None = None,
) -> dict[str, str | None]:
    stmt = select(
        func.min(ParlourMilkFlowRow.milking_date),
        func.max(ParlourMilkFlowRow.milking_date),
    )
    if farm:
        stmt = stmt.where(ParlourMilkFlowRow.farm == farm.upper())
    mn, mx = db.execute(stmt).one()
    return {
        "date_min": mn.isoformat() if mn else None,
        "date_max": mx.isoformat() if mx else None,
    }


def _wall_ms(milking_date: dt.date, abs_seconds: int) -> int:
    """Encode absolute shift timeline seconds as UTC-epoch of wall-clock time."""
    day_offset, secs = divmod(int(abs_seconds), 86400)
    day = milking_date + dt.timedelta(days=day_offset)
    started = dt.datetime.combine(day, dt.time.min) + dt.timedelta(seconds=secs)
    return int((started - dt.datetime(1970, 1, 1)).total_seconds() * 1000)


def _moving_average(
    points: list[tuple[int, float]],
    window: int,
) -> list[dict[str, float | int]]:
    """Centered-ish trailing MA over sorted (x_ms, gap_seconds) points."""
    if window < 1 or not points:
        return []
    out: list[dict[str, float | int]] = []
    vals: list[float] = []
    for i, (x_ms, gap) in enumerate(points):
        vals.append(gap)
        if len(vals) > window:
            vals.pop(0)
        if len(vals) < max(3, min(window, 10)):
            # Need a small burn-in before publishing the average.
            continue
        out.append({"x": x_ms, "y": round(fmean(vals) / 60.0, 2)})
    return out


def list_rotation_series(
    db: Session,
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
    ma_window: int = DEFAULT_MA_WINDOW,
    min_seconds: int = MIN_ROTATION_SECONDS,
    max_seconds: int = MAX_ROTATION_SECONDS,
) -> dict[str, Any]:
    farm_key = farm.upper()
    window = max(5, min(int(ma_window or DEFAULT_MA_WINDOW), 500))
    min_s = max(60, int(min_seconds))
    max_s = max(min_s + 60, int(max_seconds))

    if shifts is not None and len(shifts) == 0:
        bounds = rotation_date_bounds(db, farm=farm_key)
        return {
            "farm": farm_key,
            "date_min": bounds["date_min"],
            "date_max": bounds["date_max"],
            "gap_count_raw": 0,
            "gap_count_clean": 0,
            "median_rotation_minutes": None,
            "mean_rotation_minutes": None,
            "ma_window": window,
            "min_seconds": min_s,
            "max_seconds": max_s,
            "points": [],
            "moving_average": [],
            "truncated": False,
        }

    stmt = select(
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.shift,
        ParlourMilkFlowRow.milking_point,
        ParlourMilkFlowRow.start_seconds,
    ).where(
        ParlourMilkFlowRow.farm == farm_key,
        ParlourMilkFlowRow.milking_point.isnot(None),
        ParlourMilkFlowRow.start_seconds.isnot(None),
    )
    if date_from:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date >= date_from)
    if date_to:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)
    if shifts:
        stmt = stmt.where(ParlourMilkFlowRow.shift.in_(shifts))

    rows = list(db.execute(stmt).all())

    # Group by shift day so midnight wrap is handled per milking session.
    by_session: dict[tuple[dt.date, str], list[tuple[int, int]]] = defaultdict(list)
    for milking_date, shift, point, start_seconds in rows:
        by_session[(milking_date, shift)].append((int(point), int(start_seconds)))

    raw_gaps: list[tuple[int, float, int]] = []  # x_ms, gap_s, point
    for (milking_date, _shift), items in sorted(by_session.items()):
        starts = [s for _, s in items]
        origin = shift_timeline_origin(starts)
        if origin is None:
            continue
        by_point: dict[int, list[int]] = defaultdict(list)
        for point, start in items:
            by_point[point].append(to_absolute_start(start, origin))

        for point, abs_starts in by_point.items():
            ordered = sorted(abs_starts)
            for prev, nxt in zip(ordered, ordered[1:]):
                gap = nxt - prev
                x_ms = _wall_ms(milking_date, nxt)
                raw_gaps.append((x_ms, float(gap), point))

    raw_gaps.sort(key=lambda g: g[0])
    gap_count_raw = len(raw_gaps)
    clean = [
        (x, gap, pt)
        for x, gap, pt in raw_gaps
        if min_s <= gap <= max_s
    ]
    clean_gaps = [gap for _, gap, _ in clean]
    gap_count_clean = len(clean)

    # Compute MA on full cleaned series, then optionally thin both for the UI.
    ma_full = _moving_average([(x, gap) for x, gap, _ in clean], window)
    truncated = len(clean) > MAX_POINTS
    plot_clean = clean
    plot_ma = ma_full
    if truncated:
        step = max(1, len(clean) // MAX_POINTS)
        plot_clean = clean[::step][:MAX_POINTS]
        if len(ma_full) > MAX_POINTS:
            ma_step = max(1, len(ma_full) // MAX_POINTS)
            plot_ma = ma_full[::ma_step][:MAX_POINTS]

    points = [
        {
            "x": x,
            "y": round(gap / 60.0, 2),
            "milking_point": pt,
        }
        for x, gap, pt in plot_clean
    ]

    bounds = rotation_date_bounds(db, farm=farm_key)
    return {
        "farm": farm_key,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "gap_count_raw": gap_count_raw,
        "gap_count_clean": gap_count_clean,
        "median_rotation_minutes": (
            round(median(clean_gaps) / 60.0, 2) if clean_gaps else None
        ),
        "mean_rotation_minutes": (
            round(fmean(clean_gaps) / 60.0, 2) if clean_gaps else None
        ),
        "ma_window": window,
        "min_seconds": min_s,
        "max_seconds": max_s,
        "points": points,
        "moving_average": plot_ma,
        "truncated": truncated,
    }
