"""Scatter-plot data for parlour milk-flow cow rows."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import fmean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowRow
from app.services.parlour_metric_cleaning import (
    FLOW_RATE_METRICS,
    scatter_metric_value,
)
from app.services.parlour_milk_flow_parse import (
    shift_timeline_origin,
    to_absolute_start,
)

_SHIFT_SORT = {"Morning": 0, "Day": 1, "Afternoon": 2, "Evening": 3, "Night": 4}
_ATTACHMENT_BIN_SECONDS = 300  # 5-minute bins for attachment-time histogram
_ATTACHMENT_GAP_SECONDS = 300  # flag gaps longer than one bin between attachments
ATTACHMENT_METRIC_KEY = "attachments"

# Per-cow metrics available on the scatter page.
SCATTER_METRICS: dict[str, dict[str, Any]] = {
    "yield_kg": {"label": "Yield (kg)", "unit": "kg", "digits": 2},
    "duration_seconds": {
        "label": "Unit On Time (min)",
        "unit": "min",
        "digits": 1,
        "scale": 1 / 60.0,
    },
    "lag_phase_seconds": {
        "label": "Lag phase (s)",
        "unit": "s",
        "digits": 0,
    },
    "average_flow": {"label": "Average Flow", "unit": "", "digits": 2},
    "peak_flow": {"label": "Peak Flow", "unit": "", "digits": 2},
    "flow_15s": {"label": "15s flow", "unit": "", "digits": 1},
    "flow_30s": {"label": "30s flow", "unit": "", "digits": 1},
    "flow_60s": {"label": "60s flow", "unit": "", "digits": 1},
    "flow_120s": {"label": "120s flow", "unit": "", "digits": 1},
    "pct_2_minutes": {"label": "% in 2 minutes", "unit": "%", "digits": 1},
    "milk_yield_2_minutes": {
        "label": "Milk yield at 2 min (kg)",
        "unit": "kg",
        "digits": 2,
    },
    "flow_rate_at_removal": {
        "label": "Takeoff Flow",
        "unit": "",
        "digits": 1,
    },
}

SCATTER_METRIC_KEYS = frozenset(SCATTER_METRICS)


def list_scatter_metrics() -> list[dict[str, str]]:
    return [
        {
            "key": ATTACHMENT_METRIC_KEY,
            "label": "Attachments (5-min bins)",
            "chart": "bars",
        },
        *[
            {"key": key, "label": meta["label"], "chart": "scatter"}
            for key, meta in SCATTER_METRICS.items()
        ],
    ]


def _metric_column(metric: str):
    col = getattr(ParlourMilkFlowRow, metric, None)
    if col is None:
        raise ValueError(f"Unsupported metric: {metric}")
    return col


def scatter_date_bounds(
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


def list_scatter_points(
    db: Session,
    *,
    farm: str,
    metric: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
) -> dict[str, Any]:
    if metric not in SCATTER_METRIC_KEYS:
        raise ValueError(f"Unsupported metric: {metric}")

    farm_key = farm.upper()
    meta = SCATTER_METRICS[metric]
    scale = float(meta.get("scale") or 1.0)
    metric_col = _metric_column(metric)
    needs_peak = metric == "average_flow"

    # Empty shift list = intentionally no points.
    if shifts is not None and len(shifts) == 0:
        bounds = scatter_date_bounds(db, farm=farm_key)
        return {
            "farm": farm_key,
            "metric": metric,
            "metric_label": meta["label"],
            "unit": meta["unit"],
            "digits": meta["digits"],
            "date_min": bounds["date_min"],
            "date_max": bounds["date_max"],
            "point_count": 0,
            "truncated": False,
            "points": [],
            "shift_day_averages": [],
        }

    needs_yield = metric in FLOW_RATE_METRICS
    cols = [
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.shift,
        ParlourMilkFlowRow.start_seconds,
        ParlourMilkFlowRow.cow_id,
        ParlourMilkFlowRow.milking_point,
        metric_col,
    ]
    if needs_peak:
        cols.append(ParlourMilkFlowRow.peak_flow)
    if needs_yield:
        cols.append(ParlourMilkFlowRow.yield_kg)

    stmt = select(*cols).where(
        ParlourMilkFlowRow.farm == farm_key,
        ParlourMilkFlowRow.start_seconds.isnot(None),
        metric_col.isnot(None),
    )
    if date_from:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date >= date_from)
    if date_to:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)
    if shifts:
        stmt = stmt.where(ParlourMilkFlowRow.shift.in_(shifts))

    stmt = stmt.order_by(
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.start_seconds,
    )

    rows = list(db.execute(stmt).all())

    points: list[dict[str, Any]] = []
    # (date, shift) -> list of (start_seconds, y)
    buckets: dict[tuple[dt.date, str], list[tuple[int, float]]] = defaultdict(list)
    digits = int(meta["digits"])

    for row in rows:
        peak = None
        yield_kg = None
        if needs_peak and needs_yield:
            (
                milking_date,
                shift,
                start_seconds,
                cow_id,
                milking_point,
                raw,
                peak,
                yield_kg,
            ) = row
        elif needs_peak:
            milking_date, shift, start_seconds, cow_id, milking_point, raw, peak = row
        elif needs_yield:
            (
                milking_date,
                shift,
                start_seconds,
                cow_id,
                milking_point,
                raw,
                yield_kg,
            ) = row
        else:
            milking_date, shift, start_seconds, cow_id, milking_point, raw = row
        if start_seconds is None or raw is None:
            continue
        cleaned = scatter_metric_value(
            metric, raw, peak_flow=peak, yield_kg=yield_kg
        )
        if cleaned is None:
            continue
        started = dt.datetime.combine(milking_date, dt.time.min) + dt.timedelta(
            seconds=int(start_seconds)
        )
        # Treat milking clock time as absolute wall-clock (no server TZ shift).
        x_ms = int((started - dt.datetime(1970, 1, 1)).total_seconds() * 1000)
        y = float(cleaned) * scale
        buckets[(milking_date, shift)].append((int(start_seconds), y))
        points.append(
            {
                "x": x_ms,
                "y": round(y, digits + 2),
                "shift": shift,
                "milking_date": milking_date.isoformat(),
                "start_seconds": int(start_seconds),
                "cow_id": cow_id,
                "milking_point": milking_point,
            }
        )

    shift_day_averages: list[dict[str, Any]] = []
    for milking_date, shift in sorted(
        buckets.keys(),
        key=lambda key: (key[0], _SHIFT_SORT.get(key[1], 99), key[1]),
    ):
        vals = buckets[(milking_date, shift)]
        if not vals:
            continue
        mean_start = int(round(fmean(s for s, _ in vals)))
        mean_y = fmean(y for _, y in vals)
        started = dt.datetime.combine(milking_date, dt.time.min) + dt.timedelta(
            seconds=mean_start
        )
        x_ms = int((started - dt.datetime(1970, 1, 1)).total_seconds() * 1000)
        shift_day_averages.append(
            {
                "x": x_ms,
                "y": round(mean_y, digits + 2),
                "shift": shift,
                "milking_date": milking_date.isoformat(),
                "n": len(vals),
            }
        )

    bounds = scatter_date_bounds(db, farm=farm_key)
    return {
        "farm": farm_key,
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
        "digits": meta["digits"],
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "point_count": len(points),
        "truncated": False,
        "points": points,
        "shift_day_averages": shift_day_averages,
    }


def _wall_clock_ms(milking_date: dt.date, seconds: int) -> int:
    started = dt.datetime.combine(milking_date, dt.time.min) + dt.timedelta(
        seconds=int(seconds)
    )
    return int((started - dt.datetime(1970, 1, 1)).total_seconds() * 1000)


def list_attachment_time_bins(
    db: Session,
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
    bin_seconds: int = _ATTACHMENT_BIN_SECONDS,
) -> dict[str, Any]:
    """Count cow attachments in fixed time bins; flag gaps longer than 5 minutes."""
    farm_key = farm.upper()
    bin_width = bin_seconds if bin_seconds and bin_seconds > 0 else _ATTACHMENT_BIN_SECONDS
    bounds = scatter_date_bounds(db, farm=farm_key)

    empty = {
        "farm": farm_key,
        "metric": ATTACHMENT_METRIC_KEY,
        "metric_label": "Attachments (5-min bins)",
        "chart": "bars",
        "bin_seconds": bin_width,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "attachment_count": 0,
        "gap_count": 0,
        "bins": [],
        "gaps": [],
    }

    if shifts is not None and len(shifts) == 0:
        return empty

    stmt = select(
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.shift,
        ParlourMilkFlowRow.start_seconds,
    ).where(
        ParlourMilkFlowRow.farm == farm_key,
        ParlourMilkFlowRow.start_seconds.isnot(None),
    )
    if date_from:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date >= date_from)
    if date_to:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)
    if shifts:
        stmt = stmt.where(ParlourMilkFlowRow.shift.in_(shifts))

    counts: dict[tuple[dt.date, int, str], int] = defaultdict(int)
    by_session: dict[tuple[dt.date, str], list[int]] = defaultdict(list)
    for milking_date, shift, start_seconds in db.execute(stmt).all():
        if start_seconds is None or not shift:
            continue
        start_s = int(start_seconds)
        bin_start = (start_s // bin_width) * bin_width
        counts[(milking_date, bin_start, shift)] += 1
        by_session[(milking_date, shift)].append(start_s)

    bins: list[dict[str, Any]] = []
    total = 0
    for milking_date, bin_start, shift in sorted(
        counts.keys(),
        key=lambda key: (key[0], key[1], _SHIFT_SORT.get(key[2], 99), key[2]),
    ):
        count = counts[(milking_date, bin_start, shift)]
        total += count
        bins.append(
            {
                "x": _wall_clock_ms(milking_date, bin_start),
                "y": count,
                "shift": shift,
                "milking_date": milking_date.isoformat(),
                "bin_seconds": bin_start,
            }
        )

    gaps: list[dict[str, Any]] = []
    for milking_date, shift in sorted(
        by_session.keys(),
        key=lambda key: (key[0], _SHIFT_SORT.get(key[1], 99), key[1]),
    ):
        starts = by_session[(milking_date, shift)]
        origin = shift_timeline_origin(starts)
        if origin is None:
            continue
        abs_starts = sorted(to_absolute_start(s, origin) for s in starts)
        for earlier, later in zip(abs_starts, abs_starts[1:]):
            delta = later - earlier
            if delta <= _ATTACHMENT_GAP_SECONDS:
                continue
            mid = (earlier + later) // 2
            gap_minutes = int(round(delta / 60.0))
            gaps.append(
                {
                    "x": _wall_clock_ms(milking_date, mid),
                    "gap_minutes": gap_minutes,
                    "gap_seconds": delta,
                    "shift": shift,
                    "milking_date": milking_date.isoformat(),
                }
            )

    return {
        "farm": farm_key,
        "metric": ATTACHMENT_METRIC_KEY,
        "metric_label": "Attachments (5-min bins)",
        "chart": "bars",
        "bin_seconds": bin_width,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "attachment_count": total,
        "gap_count": len(gaps),
        "bins": bins,
        "gaps": gaps,
    }
