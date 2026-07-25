"""Scatter-plot data for parlour milk-flow cow rows."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import fmean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowRow
from app.services.parlour_metric_cleaning import scatter_metric_value

_SHIFT_SORT = {"Morning": 0, "Day": 1, "Afternoon": 2, "Evening": 3, "Night": 4}

# Per-cow metrics available on the scatter page.
SCATTER_METRICS: dict[str, dict[str, Any]] = {
    "yield_kg": {"label": "Yield (kg)", "unit": "kg", "digits": 2},
    "duration_seconds": {
        "label": "Unit On Time (min)",
        "unit": "min",
        "digits": 1,
        "scale": 1 / 60.0,
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
        {"key": key, "label": meta["label"]}
        for key, meta in SCATTER_METRICS.items()
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
        if needs_peak:
            milking_date, shift, start_seconds, cow_id, milking_point, raw, peak = row
        else:
            milking_date, shift, start_seconds, cow_id, milking_point, raw = row
            peak = None
        if start_seconds is None or raw is None:
            continue
        cleaned = scatter_metric_value(metric, raw, peak_flow=peak)
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
