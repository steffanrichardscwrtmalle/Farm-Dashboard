"""Parlour shift summary aggregates from imported milk-flow rows."""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_milk_flow_parse import (
    correct_pens_by_milking_cohort,
    milking_span_seconds,
    pen_session_span_seconds,
    shift_timeline_origin,
    to_absolute_start,
)

# Flow rate at removal above this = high-flow takeoff.
HIGH_FLOW_TAKEOFF_THRESHOLD = 1800.0

# Stall outlier rules (match Performance UI). Bad direction vs peer stalls.
OUTLIER_SD = 2.0
OUTLIER_MIN_N = 5
METRIC_OUTLIER_RULES: list[tuple[str, str]] = [
    ("avg_yield_kg", "low"),
    ("cows_per_hour", "low"),
    ("high_flow_takeoff_pct", "high"),
    ("bimodal_pct", "high"),
    ("median_milking_duration_seconds", "high"),
    ("avg_milking_duration_seconds", "high"),
    ("avg_flow_15s", "low"),
    ("avg_flow_30s", "low"),
    ("avg_flow_60s", "low"),
    ("avg_flow_120s", "low"),
    ("avg_peak_flow", "low"),
    ("avg_average_flow", "low"),
    ("avg_pct_2_minutes", "low"),
    ("avg_milk_yield_2_minutes", "low"),
    ("avg_flow_rate_at_removal", "high"),
]
SHIFT_SEQUENCE = ("Morning", "Day", "Afternoon", "Evening", "Night")


def _fmt_hhmmss(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    total = int(round(seconds))
    # Allow end times past 24h when span crossed midnight
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _fmt_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def _cows_per_hour(cow_count: int, span_seconds: int | None) -> float | None:
    if not span_seconds or span_seconds <= 0 or cow_count <= 0:
        return None
    return round(cow_count / (span_seconds / 3600.0), 1)


def _mean(values: list[float], *, digits: int = 2) -> float | None:
    if not values:
        return None
    return round(statistics.fmean(values), digits)


def _median(values: list[float], *, digits: int = 2) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), digits)


def _pct(count: int, total: int, *, digits: int = 1) -> float | None:
    if total <= 0:
        return None
    return round(100.0 * count / total, digits)


def _is_bimodal(
    flow_15s: float | None,
    flow_30s: float | None,
    flow_60s: float | None,
) -> bool | None:
    """Bi-modal let-down: 30s < 15s, or 60s < 15s, or 60s < 30s.

    Returns None when any required flow is missing.
    """
    if flow_15s is None or flow_30s is None or flow_60s is None:
        return None
    return (
        flow_30s < flow_15s
        or flow_60s < flow_15s
        or flow_60s < flow_30s
    )


def _cow_quality_stats(cows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cow milking quality aggregates for a shift."""
    durations = [
        float(c["duration_seconds"])
        for c in cows
        if c.get("duration_seconds") is not None
    ]
    flow_15 = [float(c["flow_15s"]) for c in cows if c.get("flow_15s") is not None]
    flow_30 = [float(c["flow_30s"]) for c in cows if c.get("flow_30s") is not None]
    flow_60 = [float(c["flow_60s"]) for c in cows if c.get("flow_60s") is not None]
    flow_120 = [float(c["flow_120s"]) for c in cows if c.get("flow_120s") is not None]
    pct_2 = [float(c["pct_2_minutes"]) for c in cows if c.get("pct_2_minutes") is not None]
    yield_2 = [
        float(c["milk_yield_2_minutes"])
        for c in cows
        if c.get("milk_yield_2_minutes") is not None
    ]
    removal = [
        float(c["flow_rate_at_removal"])
        for c in cows
        if c.get("flow_rate_at_removal") is not None
    ]
    avg_flow = [
        float(c["average_flow"]) for c in cows if c.get("average_flow") is not None
    ]
    peak_flow = [float(c["peak_flow"]) for c in cows if c.get("peak_flow") is not None]

    high_flow_n = sum(1 for v in removal if v > HIGH_FLOW_TAKEOFF_THRESHOLD)
    bimodal_flags = [
        _is_bimodal(c.get("flow_15s"), c.get("flow_30s"), c.get("flow_60s"))
        for c in cows
    ]
    bimodal_known = [f for f in bimodal_flags if f is not None]
    bimodal_n = sum(1 for f in bimodal_known if f)

    median_dur = _median(durations, digits=0)
    avg_dur = _mean(durations, digits=0)

    return {
        "median_milking_duration_seconds": int(median_dur) if median_dur is not None else None,
        "median_milking_duration_label": _fmt_duration(
            int(median_dur) if median_dur is not None else None
        ),
        "avg_milking_duration_seconds": int(avg_dur) if avg_dur is not None else None,
        "avg_milking_duration_label": _fmt_duration(
            int(avg_dur) if avg_dur is not None else None
        ),
        "avg_flow_15s": _mean(flow_15, digits=1),
        "avg_flow_30s": _mean(flow_30, digits=1),
        "avg_flow_60s": _mean(flow_60, digits=1),
        "avg_flow_120s": _mean(flow_120, digits=1),
        "avg_pct_2_minutes": _mean(pct_2, digits=1),
        "avg_milk_yield_2_minutes": _mean(yield_2, digits=2),
        "avg_flow_rate_at_removal": _mean(removal, digits=1),
        "avg_average_flow": _mean(avg_flow, digits=2),
        "avg_peak_flow": _mean(peak_flow, digits=2),
        "high_flow_takeoff_pct": _pct(high_flow_n, len(removal)),
        "high_flow_takeoff_count": high_flow_n,
        "high_flow_takeoff_n": len(removal),
        "bimodal_pct": _pct(bimodal_n, len(bimodal_known)),
        "bimodal_count": bimodal_n,
        "bimodal_n": len(bimodal_known),
    }


def _pen_summary(
    rows: list[ParlourMilkFlowRow],
    corrected_pens: list[int | None],
    abs_starts: list[int | None],
) -> list[dict]:
    by_pen: dict[int | None, list[tuple[ParlourMilkFlowRow, int]]] = defaultdict(list)
    for row, pen, abs_start in zip(rows, corrected_pens, abs_starts):
        if abs_start is None:
            continue
        by_pen[pen].append((row, abs_start))

    pens: list[dict] = []
    for pen_key in sorted(by_pen.keys(), key=lambda p: (p is None, p or 0)):
        pen_items = by_pen[pen_key]
        pairs = [
            (abs_start, row.duration_seconds or 0)
            for row, abs_start in pen_items
        ]
        first_start, last_end, span, sessions = pen_session_span_seconds(pairs)
        yield_kg = sum(row.yield_kg or 0.0 for row, _ in pen_items)
        cow_count = len(pen_items)
        pen_row = {
            "pen": pen_key,
            "cow_count": cow_count,
            "yield_kg": round(yield_kg, 1),
            "avg_yield_kg": round(yield_kg / cow_count, 2) if cow_count else None,
            "first_start": _fmt_hhmmss(first_start),
            "last_end": _fmt_hhmmss(
                last_end % 86400 if last_end is not None else None
            ),
            "duration_seconds": span,
            "duration_label": _fmt_duration(span),
            "sessions": sessions,
            "cows_per_hour": _cows_per_hour(cow_count, span),
        }
        pen_row.update(_cow_quality_stats([_row_to_cow_dict(row) for row, _ in pen_items]))
        pens.append(pen_row)
    return pens


def _milking_point_summary(rows: list[ParlourMilkFlowRow]) -> list[dict]:
    """Same quality metrics as pens, grouped by milking point (stall)."""
    by_point: dict[int | None, list[ParlourMilkFlowRow]] = defaultdict(list)
    for row in rows:
        by_point[row.milking_point].append(row)

    points: list[dict] = []
    for point_key in sorted(by_point.keys(), key=lambda p: (p is None, p or 0)):
        items = by_point[point_key]
        pairs = [
            (row.start_seconds, row.duration_seconds)
            for row in items
            if row.start_seconds is not None
        ]
        first_start, last_end, span = milking_span_seconds(pairs)
        yield_kg = sum(row.yield_kg or 0.0 for row in items)
        cow_count = len(items)
        point_row = {
            "milking_point": point_key,
            "cow_count": cow_count,
            "yield_kg": round(yield_kg, 1),
            "avg_yield_kg": round(yield_kg / cow_count, 2) if cow_count else None,
            "first_start": _fmt_hhmmss(first_start),
            "last_end": _fmt_hhmmss(
                last_end % 86400 if last_end is not None else None
            ),
            "duration_seconds": span,
            "duration_label": _fmt_duration(span),
            "cows_per_hour": _cows_per_hour(cow_count, span),
        }
        point_row.update(_cow_quality_stats([_row_to_cow_dict(row) for row in items]))
        points.append(point_row)
    return points


def _sample_stats(values: list[float]) -> tuple[float, float] | None:
    if len(values) < OUTLIER_MIN_N:
        return None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if not sd or sd <= 0:
        return None
    return mean, float(sd)


def _is_outlier_bad(
    value: float | None,
    stats: tuple[float, float] | None,
    bad_direction: str,
) -> bool:
    if value is None or stats is None:
        return False
    mean, sd = stats
    z = (float(value) - mean) / sd
    if bad_direction == "high":
        return z >= OUTLIER_SD
    if bad_direction == "low":
        return z <= -OUTLIER_SD
    return False


def _previous_shift(milking_date: dt.date, shift: str) -> tuple[dt.date, str]:
    if shift in SHIFT_SEQUENCE:
        idx = SHIFT_SEQUENCE.index(shift)
    else:
        idx = 0
    if idx > 0:
        return milking_date, SHIFT_SEQUENCE[idx - 1]
    return milking_date - dt.timedelta(days=1), SHIFT_SEQUENCE[-1]


def _alert_metrics_for_points(points: list[dict[str, Any]]) -> dict[Any, set[str]]:
    """Map milking_point -> set of metric keys that are ≥2 SD bad within this shift."""
    alerts: dict[Any, set[str]] = defaultdict(set)
    if len(points) < OUTLIER_MIN_N:
        return alerts
    for metric, bad_direction in METRIC_OUTLIER_RULES:
        values = [
            float(p[metric])
            for p in points
            if p.get(metric) is not None
        ]
        stats = _sample_stats(values)
        if stats is None:
            continue
        for p in points:
            raw = p.get(metric)
            if raw is None:
                continue
            if _is_outlier_bad(float(raw), stats, bad_direction):
                alerts[p.get("milking_point")].add(metric)
    return alerts


def _annotate_milking_point_outliers(
    by_shift: dict[tuple[dt.date, str], list[dict[str, Any]]],
) -> dict[tuple[dt.date, str], int]:
    """Add outlier_flags to each point; return problem_stall_count per (date, shift).

    alert = ≥2 SD bad this shift
    problem = alert this shift AND alert on same metric in the previous shift
    """
    alert_by_shift: dict[tuple[dt.date, str], dict[Any, set[str]]] = {}
    for key, points in by_shift.items():
        alert_by_shift[key] = _alert_metrics_for_points(points)

    problem_counts: dict[tuple[dt.date, str], int] = {}
    for key, points in by_shift.items():
        milking_date, shift = key
        prev_key = _previous_shift(milking_date, shift)
        prev_alerts = alert_by_shift.get(prev_key, {})
        cur_alerts = alert_by_shift.get(key, {})
        problem_points: set[Any] = set()
        for p in points:
            point_id = p.get("milking_point")
            flags: dict[str, str] = {}
            for metric in cur_alerts.get(point_id, set()):
                if metric in prev_alerts.get(point_id, set()):
                    flags[metric] = "problem"
                    problem_points.add(point_id)
                else:
                    flags[metric] = "alert"
            p["outlier_flags"] = flags
        problem_counts[key] = len(problem_points)
    return problem_counts


def _shift_summaries_from_point_rows(
    rows: list[ParlourMilkFlowRow],
) -> dict[tuple[str, dt.date, str], list[dict[str, Any]]]:
    """farm, date, shift -> milking point summaries."""
    grouped: dict[tuple[str, dt.date, str], list[ParlourMilkFlowRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.farm, row.milking_date, row.shift)].append(row)
    out: dict[tuple[str, dt.date, str], list[dict[str, Any]]] = {}
    for key, shift_rows in grouped.items():
        out[key] = _milking_point_summary(shift_rows)
    return out


def _shift_summary_core(
    *,
    yield_kg: float,
    cow_count: int,
    start_duration_pairs: list[tuple[int, int | None]],
    cows: list[dict[str, Any]],
) -> dict:
    first_start, last_end, span = milking_span_seconds(start_duration_pairs)
    summary = {
        "cow_count": cow_count,
        "yield_kg": round(yield_kg, 1),
        "avg_yield_kg": round(yield_kg / cow_count, 2) if cow_count else None,
        "first_start": _fmt_hhmmss(first_start),
        "last_end": _fmt_hhmmss(last_end % 86400 if last_end is not None else None),
        "duration_seconds": span,
        "duration_label": _fmt_duration(span),
        "duration_hours": round(span / 3600.0, 2) if span else None,
        "cows_per_hour": _cows_per_hour(cow_count, span),
        "pen_corrections": 0,
        "pens": [],
        "milking_points": [],
        "problem_stall_count": None,
    }
    summary.update(_cow_quality_stats(cows))
    return summary


def _row_to_cow_dict(row: ParlourMilkFlowRow) -> dict[str, Any]:
    return {
        "duration_seconds": row.duration_seconds,
        "flow_15s": row.flow_15s,
        "flow_30s": row.flow_30s,
        "flow_60s": row.flow_60s,
        "flow_120s": row.flow_120s,
        "pct_2_minutes": row.pct_2_minutes,
        "milk_yield_2_minutes": row.milk_yield_2_minutes,
        "flow_rate_at_removal": row.flow_rate_at_removal,
        "average_flow": row.average_flow,
        "peak_flow": row.peak_flow,
    }


def _shift_summary(
    rows: list[ParlourMilkFlowRow],
    *,
    include_pens: bool = False,
    include_milking_points: bool = False,
) -> dict:
    pairs = [
        (r.start_seconds, r.duration_seconds)
        for r in rows
        if r.start_seconds is not None
    ]
    yield_kg = sum(r.yield_kg or 0.0 for r in rows)
    cow_count = len(rows)
    summary = _shift_summary_core(
        yield_kg=yield_kg,
        cow_count=cow_count,
        start_duration_pairs=pairs,
        cows=[_row_to_cow_dict(r) for r in rows],
    )
    if include_pens:
        origin = shift_timeline_origin(
            [r.start_seconds for r in rows if r.start_seconds is not None]
        )
        abs_starts: list[int | None] = []
        pens: list[int | None] = []
        for row in rows:
            if row.start_seconds is None or origin is None:
                abs_starts.append(None)
            else:
                abs_starts.append(to_absolute_start(row.start_seconds, origin))
            pens.append(row.pen)

        correctable_idx = [i for i, a in enumerate(abs_starts) if a is not None]
        corrected = list(pens)
        pen_corrections = 0
        if correctable_idx:
            sub_pens = [pens[i] for i in correctable_idx]
            sub_abs = [abs_starts[i] for i in correctable_idx]  # type: ignore[misc]
            sub_corrected, pen_corrections = correct_pens_by_milking_cohort(
                sub_pens, sub_abs  # type: ignore[arg-type]
            )
            for i, new_pen in zip(correctable_idx, sub_corrected):
                corrected[i] = new_pen

        summary["pen_corrections"] = pen_corrections
        summary["pens"] = _pen_summary(rows, corrected, abs_starts)

    if include_milking_points:
        summary["milking_points"] = _milking_point_summary(rows)

    return summary


def list_shift_summaries(
    db: Session,
    *,
    farm: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shift: str | None = None,
    include_pens: bool = False,
    include_milking_points: bool = False,
    include_problem_stalls: bool = False,
) -> dict:
    need_points = include_milking_points or include_problem_stalls
    need_orm = include_pens or need_points
    # Need prior calendar day so Morning can compare to previous Night.
    requested_from = date_from
    query_from = (
        date_from - dt.timedelta(days=1)
        if need_points and date_from is not None
        else date_from
    )
    # Keep all shifts when annotating problems so "previous shift" exists.
    query_shift = None if need_points else shift

    if not need_orm:
        stmt = select(
            ParlourMilkFlowRow.farm,
            ParlourMilkFlowRow.milking_date,
            ParlourMilkFlowRow.shift,
            ParlourMilkFlowRow.start_seconds,
            ParlourMilkFlowRow.duration_seconds,
            ParlourMilkFlowRow.yield_kg,
            ParlourMilkFlowRow.flow_15s,
            ParlourMilkFlowRow.flow_30s,
            ParlourMilkFlowRow.flow_60s,
            ParlourMilkFlowRow.flow_120s,
            ParlourMilkFlowRow.pct_2_minutes,
            ParlourMilkFlowRow.milk_yield_2_minutes,
            ParlourMilkFlowRow.flow_rate_at_removal,
            ParlourMilkFlowRow.average_flow,
            ParlourMilkFlowRow.peak_flow,
        )
        if farm:
            stmt = stmt.where(ParlourMilkFlowRow.farm == farm.upper())
        if query_from:
            stmt = stmt.where(ParlourMilkFlowRow.milking_date >= query_from)
        if date_to:
            stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)
        if query_shift:
            stmt = stmt.where(ParlourMilkFlowRow.shift == query_shift)

        grouped_vals: dict[tuple[str, dt.date, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for (
            farm_key,
            milking_date,
            shift_name,
            start_s,
            dur_s,
            yield_kg,
            flow_15s,
            flow_30s,
            flow_60s,
            flow_120s,
            pct_2_minutes,
            milk_yield_2_minutes,
            flow_rate_at_removal,
            average_flow,
            peak_flow,
        ) in db.execute(stmt):
            grouped_vals[(farm_key, milking_date, shift_name)].append(
                {
                    "start_seconds": start_s,
                    "duration_seconds": dur_s,
                    "yield_kg": yield_kg,
                    "flow_15s": flow_15s,
                    "flow_30s": flow_30s,
                    "flow_60s": flow_60s,
                    "flow_120s": flow_120s,
                    "pct_2_minutes": pct_2_minutes,
                    "milk_yield_2_minutes": milk_yield_2_minutes,
                    "flow_rate_at_removal": flow_rate_at_removal,
                    "average_flow": average_flow,
                    "peak_flow": peak_flow,
                }
            )

        days: dict[tuple[str, dt.date], dict] = {}
        for (farm_key, milking_date, shift_name), values in sorted(
            grouped_vals.items(),
            key=lambda item: (item[0][1], item[0][0], item[0][2]),
            reverse=True,
        ):
            day_key = (farm_key, milking_date)
            if day_key not in days:
                days[day_key] = {
                    "farm": farm_key,
                    "milking_date": milking_date.isoformat(),
                    "shifts": [],
                    "total_yield_kg": 0.0,
                    "total_cows": 0,
                }
            pairs = [
                (c["start_seconds"], c["duration_seconds"])
                for c in values
                if c["start_seconds"] is not None
            ]
            yield_total = sum(c["yield_kg"] or 0.0 for c in values)
            summary = _shift_summary_core(
                yield_kg=yield_total,
                cow_count=len(values),
                start_duration_pairs=pairs,
                cows=values,
            )
            days[day_key]["shifts"].append({"shift": shift_name, **summary})
            days[day_key]["total_yield_kg"] = round(
                days[day_key]["total_yield_kg"] + summary["yield_kg"], 1
            )
            days[day_key]["total_cows"] += summary["cow_count"]
    else:
        stmt_orm = select(ParlourMilkFlowRow)
        if farm:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.farm == farm.upper())
        if query_from:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.milking_date >= query_from)
        if date_to:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.milking_date <= date_to)
        if query_shift:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.shift == query_shift)

        rows = list(db.scalars(stmt_orm).all())
        grouped: dict[tuple[str, dt.date, str], list[ParlourMilkFlowRow]] = defaultdict(
            list
        )
        for row in rows:
            grouped[(row.farm, row.milking_date, row.shift)].append(row)

        days = {}
        for (farm_key, milking_date, shift_name), shift_rows in sorted(
            grouped.items(),
            key=lambda item: (item[0][1], item[0][0], item[0][2]),
            reverse=True,
        ):
            day_key = (farm_key, milking_date)
            if day_key not in days:
                days[day_key] = {
                    "farm": farm_key,
                    "milking_date": milking_date.isoformat(),
                    "shifts": [],
                    "total_yield_kg": 0.0,
                    "total_cows": 0,
                }
            summary = _shift_summary(
                shift_rows,
                include_pens=include_pens,
                include_milking_points=False,
            )
            days[day_key]["shifts"].append(
                {
                    "shift": shift_name,
                    **summary,
                }
            )
            days[day_key]["total_yield_kg"] = round(
                days[day_key]["total_yield_kg"] + summary["yield_kg"], 1
            )
            days[day_key]["total_cows"] += summary["cow_count"]

        if need_points:
            point_map = _shift_summaries_from_point_rows(rows)
            farms_seen = {key[0] for key in point_map}
            for farm_key in farms_seen:
                farm_points: dict[tuple[dt.date, str], list[dict[str, Any]]] = {
                    (d, s): pts
                    for (f, d, s), pts in point_map.items()
                    if f == farm_key
                }
                counts = _annotate_milking_point_outliers(farm_points)
                for day in days.values():
                    if day["farm"] != farm_key:
                        continue
                    milking_date = dt.date.fromisoformat(day["milking_date"])
                    for shift_row in day["shifts"]:
                        key = (milking_date, shift_row["shift"])
                        shift_row["problem_stall_count"] = counts.get(key, 0)
                        if include_milking_points:
                            shift_row["milking_points"] = farm_points.get(key, [])

    # Sort shifts within each day: Morning → Day → Night (legacy Evening/Afternoon kept)
    shift_order = {
        "Morning": 0,
        "Day": 1,
        "Afternoon": 2,
        "Evening": 3,
        "Night": 4,
    }
    day_list = list(days.values())
    if requested_from is not None:
        day_list = [
            d
            for d in day_list
            if dt.date.fromisoformat(d["milking_date"]) >= requested_from
        ]
    for day in day_list:
        day["shifts"].sort(
            key=lambda s: (shift_order.get(s["shift"], 99), s["shift"])
        )

    import_count = db.scalar(select(func.count()).select_from(ParlourMilkFlowImport)) or 0
    latest = db.scalar(select(func.max(ParlourMilkFlowImport.imported_at)))
    return {
        "days": day_list,
        "day_count": len(day_list),
        "import_count": import_count,
        "latest_import": latest.isoformat() if latest else None,
    }


TREND_METRIC_KEYS = frozenset(
    {
        "avg_yield_kg",
        "cows_per_hour",
        "high_flow_takeoff_pct",
        "bimodal_pct",
        "median_milking_duration_seconds",
        "avg_milking_duration_seconds",
        "avg_flow_15s",
        "avg_flow_30s",
        "avg_flow_60s",
        "avg_flow_120s",
        "avg_peak_flow",
        "avg_average_flow",
        "avg_pct_2_minutes",
        "avg_milk_yield_2_minutes",
        "avg_flow_rate_at_removal",
    }
)


def _group_metric_value(rows: list[ParlourMilkFlowRow], metric: str) -> float | None:
    if not rows or metric not in TREND_METRIC_KEYS:
        return None
    pairs = [
        (row.start_seconds, row.duration_seconds)
        for row in rows
        if row.start_seconds is not None
    ]
    _first, _last, span = milking_span_seconds(pairs)
    yield_kg = sum(row.yield_kg or 0.0 for row in rows)
    cow_count = len(rows)
    values: dict[str, Any] = {
        "cow_count": cow_count,
        "yield_kg": round(yield_kg, 1),
        "avg_yield_kg": round(yield_kg / cow_count, 2) if cow_count else None,
        "duration_seconds": span,
        "cows_per_hour": _cows_per_hour(cow_count, span),
    }
    values.update(_cow_quality_stats([_row_to_cow_dict(row) for row in rows]))
    raw = values.get(metric)
    if raw is None:
        return None
    return float(raw)


def milking_point_metric_trend(
    db: Session,
    *,
    farm: str,
    milking_point: int | None,
    metric: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Trend of one metric for one milking point, split by shift over time."""
    if metric not in TREND_METRIC_KEYS:
        raise ValueError(f"Unsupported metric: {metric}")

    farm_key = farm.upper()
    stmt = select(ParlourMilkFlowRow).where(ParlourMilkFlowRow.farm == farm_key)
    if milking_point is None:
        stmt = stmt.where(ParlourMilkFlowRow.milking_point.is_(None))
    else:
        stmt = stmt.where(ParlourMilkFlowRow.milking_point == milking_point)
    if date_from:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date >= date_from)
    if date_to:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)

    rows = list(db.scalars(stmt).all())
    grouped: dict[tuple[dt.date, str], list[ParlourMilkFlowRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.milking_date, row.shift)].append(row)

    shift_order = ("Morning", "Day", "Afternoon", "Evening", "Night")
    by_shift: dict[str, list[dict[str, Any]]] = {name: [] for name in shift_order}
    dates: set[str] = set()

    for (milking_date, shift_name), group_rows in sorted(grouped.items()):
        value = _group_metric_value(group_rows, metric)
        date_iso = milking_date.isoformat()
        dates.add(date_iso)
        if shift_name not in by_shift:
            by_shift[shift_name] = []
        by_shift[shift_name].append(
            {
                "date": date_iso,
                "value": value,
                "cow_count": len(group_rows),
            }
        )

    date_list = sorted(dates)
    series = []
    for shift_name in shift_order:
        points = by_shift.get(shift_name) or []
        if not points:
            continue
        series.append({"shift": shift_name, "points": points})
    # Any unexpected shift names
    for shift_name, points in sorted(by_shift.items()):
        if shift_name in shift_order or not points:
            continue
        series.append({"shift": shift_name, "points": points})

    return {
        "farm": farm_key,
        "milking_point": milking_point,
        "metric": metric,
        "dates": date_list,
        "series": series,
        "point_count": len(date_list),
    }
