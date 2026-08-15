"""Parlour shift summary aggregates from imported milk-flow rows."""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_metric_cleaning import (
    eligible_average_flow,
    eligible_duration_seconds,
    eligible_interval_flow,
    eligible_milk_yield_2_minutes,
    eligible_peak_flow,
    eligible_pct_2_minutes,
    eligible_takeoff_flow,
    eligible_yield_kg,
    is_bimodal,
)
from app.services.parlour_milk_flow_parse import (
    attachment_idle_from_clock_starts,
    correct_pens_by_milking_cohort,
    milking_span_seconds,
    pen_session_span_seconds,
    shift_timeline_origin,
    sum_attachment_idle_gaps,
    to_absolute_start,
)
from app.services.parlour_rotation import rotation_stats_from_point_starts

# Flow rate at removal above this = high-flow takeoff.
HIGH_FLOW_TAKEOFF_THRESHOLD = 1800.0

# Stall outlier rules. Bad direction vs peer stalls.
OUTLIER_SD = 2.0
OUTLIER_MIN_N = 5
METRIC_OUTLIER_RULES: list[tuple[str, str]] = [
    ("avg_yield_kg", "low"),
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


def _attachment_idle_fields_from_clock(
    start_seconds_list: list[int | None],
) -> dict[str, Any]:
    total, gap_n = attachment_idle_from_clock_starts(start_seconds_list)
    minutes = int(round(total / 60.0)) if total else 0
    return {
        "attachment_idle_seconds": total,
        "attachment_idle_minutes": minutes,
        "attachment_idle_label": str(minutes),
        "attachment_idle_gap_n": gap_n,
    }


def _attachment_idle_fields_from_abs(abs_starts: list[int]) -> dict[str, Any]:
    total, gap_n = sum_attachment_idle_gaps(abs_starts)
    minutes = int(round(total / 60.0)) if total else 0
    return {
        "attachment_idle_seconds": total,
        "attachment_idle_minutes": minutes,
        "attachment_idle_label": str(minutes),
        "attachment_idle_gap_n": gap_n,
    }


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


def _avg_yield_kg(yield_values: list[Any]) -> float | None:
    """Mean yield excluding null/zero (does not change total yield or cow counts)."""
    cleaned = [
        v
        for v in (eligible_yield_kg(y) for y in yield_values)
        if v is not None
    ]
    return _mean(cleaned, digits=2)


def _cow_quality_stats(cows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cow milking quality aggregates for a shift (per-metric cleaning)."""
    durations = [
        v
        for v in (eligible_duration_seconds(c.get("duration_seconds")) for c in cows)
        if v is not None
    ]
    flow_15 = [
        v
        for v in (
            eligible_interval_flow(c.get("flow_15s"), yield_kg=c.get("yield_kg"))
            for c in cows
        )
        if v is not None
    ]
    flow_30 = [
        v
        for v in (
            eligible_interval_flow(c.get("flow_30s"), yield_kg=c.get("yield_kg"))
            for c in cows
        )
        if v is not None
    ]
    flow_60 = [
        v
        for v in (
            eligible_interval_flow(c.get("flow_60s"), yield_kg=c.get("yield_kg"))
            for c in cows
        )
        if v is not None
    ]
    flow_120 = [
        v
        for v in (
            eligible_interval_flow(c.get("flow_120s"), yield_kg=c.get("yield_kg"))
            for c in cows
        )
        if v is not None
    ]
    pct_2 = [
        v
        for v in (eligible_pct_2_minutes(c.get("pct_2_minutes")) for c in cows)
        if v is not None
    ]
    yield_2 = [
        v
        for v in (
            eligible_milk_yield_2_minutes(c.get("milk_yield_2_minutes")) for c in cows
        )
        if v is not None
    ]
    removal = [
        v
        for v in (
            eligible_takeoff_flow(
                c.get("flow_rate_at_removal"), yield_kg=c.get("yield_kg")
            )
            for c in cows
        )
        if v is not None
    ]
    avg_flow = [
        v
        for v in (
            eligible_average_flow(
                c.get("average_flow"),
                c.get("peak_flow"),
                yield_kg=c.get("yield_kg"),
            )
            for c in cows
        )
        if v is not None
    ]
    peak_flow = [
        v
        for v in (
            eligible_peak_flow(c.get("peak_flow"), yield_kg=c.get("yield_kg"))
            for c in cows
        )
        if v is not None
    ]

    high_flow_n = sum(1 for v in removal if v > HIGH_FLOW_TAKEOFF_THRESHOLD)
    bimodal_flags = [
        is_bimodal(
            c.get("flow_15s"),
            c.get("flow_30s"),
            c.get("flow_60s"),
            yield_kg=c.get("yield_kg"),
        )
        for c in cows
    ]
    bimodal_known = [f for f in bimodal_flags if f is not None]
    bimodal_n = sum(1 for f in bimodal_known if f)

    median_dur = _median(durations, digits=0)
    avg_dur = _mean(durations, digits=0)
    lag_values = [
        float(v)
        for v in (c.get("lag_phase_seconds") for c in cows)
        if v is not None
    ]
    median_lag = _median(lag_values, digits=0)

    return {
        "median_milking_duration_seconds": int(median_dur) if median_dur is not None else None,
        "median_milking_duration_label": _fmt_duration(
            int(median_dur) if median_dur is not None else None
        ),
        "avg_milking_duration_seconds": int(avg_dur) if avg_dur is not None else None,
        "avg_milking_duration_label": _fmt_duration(
            int(avg_dur) if avg_dur is not None else None
        ),
        "median_lag_phase_seconds": int(median_lag) if median_lag is not None else None,
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
            "avg_yield_kg": _avg_yield_kg([row.yield_kg for row, _ in pen_items]),
            "first_start": _fmt_hhmmss(first_start),
            "last_end": _fmt_hhmmss(
                last_end % 86400 if last_end is not None else None
            ),
            "duration_seconds": span,
            "duration_label": _fmt_duration(span),
            "sessions": sessions,
            "cows_per_hour": _cows_per_hour(cow_count, span),
        }
        pen_row.update(
            _attachment_idle_fields_from_abs([abs_start for _, abs_start in pen_items])
        )
        pen_row.update(
            rotation_stats_from_point_starts(
                [(row.milking_point, row.start_seconds) for row, _ in pen_items]
            )
        )
        pen_row.update(_cow_quality_stats([_row_to_cow_dict(row) for row, _ in pen_items]))
        pens.append(pen_row)
    return pens


def _milking_point_summary_from_cows(cows: list[dict[str, Any]]) -> list[dict]:
    """Same quality metrics as pens, grouped by milking point (stall)."""
    by_point: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for cow in cows:
        by_point[cow.get("milking_point")].append(cow)

    points: list[dict] = []
    for point_key in sorted(by_point.keys(), key=lambda p: (p is None, p or 0)):
        items = by_point[point_key]
        pairs = [
            (c["start_seconds"], c["duration_seconds"])
            for c in items
            if c.get("start_seconds") is not None
        ]
        first_start, last_end, span = milking_span_seconds(pairs)
        yield_kg = sum(c.get("yield_kg") or 0.0 for c in items)
        cow_count = len(items)
        point_row = {
            "milking_point": point_key,
            "cow_count": cow_count,
            "yield_kg": round(yield_kg, 1),
            "avg_yield_kg": _avg_yield_kg([c.get("yield_kg") for c in items]),
            "first_start": _fmt_hhmmss(first_start),
            "last_end": _fmt_hhmmss(
                last_end % 86400 if last_end is not None else None
            ),
            "duration_seconds": span,
            "duration_label": _fmt_duration(span),
            "cows_per_hour": _cows_per_hour(cow_count, span),
        }
        point_row.update(
            _attachment_idle_fields_from_clock(
                [c.get("start_seconds") for c in items]
            )
        )
        point_row.update(
            rotation_stats_from_point_starts(
                [(c.get("milking_point"), c.get("start_seconds")) for c in items]
            )
        )
        point_row.update(_cow_quality_stats(items))
        points.append(point_row)
    return points


def _milking_point_summary(rows: list[ParlourMilkFlowRow]) -> list[dict]:
    """ORM wrapper around dict-based milking-point summary."""
    return _milking_point_summary_from_cows([_row_to_cow_dict(row) | {"yield_kg": row.yield_kg} for row in rows])


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


def _previous_existing_shift(
    milking_date: dt.date,
    shift: str,
    existing: set[tuple[dt.date, str]],
) -> tuple[dt.date, str] | None:
    """Walk back through SHIFT_SEQUENCE until a shift that was actually milked."""
    date_cur, shift_cur = milking_date, shift
    for _ in range(len(SHIFT_SEQUENCE) * 3):
        date_cur, shift_cur = _previous_shift(date_cur, shift_cur)
        if (date_cur, shift_cur) in existing:
            return date_cur, shift_cur
    return None


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
    problem = alert this shift AND alert on same metric in the previous
    milked shift (skips missing labels such as Evening when the farm runs
    Morning / Day / Night).
    """
    alert_by_shift: dict[tuple[dt.date, str], dict[Any, set[str]]] = {}
    for key, points in by_shift.items():
        alert_by_shift[key] = _alert_metrics_for_points(points)

    existing_keys = set(by_shift.keys())
    problem_counts: dict[tuple[dt.date, str], int] = {}
    for key, points in by_shift.items():
        milking_date, shift = key
        prev_key = _previous_existing_shift(milking_date, shift, existing_keys)
        prev_alerts = alert_by_shift.get(prev_key, {}) if prev_key else {}
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
    yield_values: list[Any],
) -> dict:
    first_start, last_end, span = milking_span_seconds(start_duration_pairs)
    summary = {
        "cow_count": cow_count,
        "yield_kg": round(yield_kg, 1),
        "avg_yield_kg": _avg_yield_kg(yield_values),
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
    summary.update(
        _attachment_idle_fields_from_clock(
            [c.get("start_seconds") for c in cows]
        )
    )
    summary.update(
        rotation_stats_from_point_starts(
            [(c.get("milking_point"), c.get("start_seconds")) for c in cows]
        )
    )
    summary.update(_cow_quality_stats(cows))
    return summary


def _row_to_cow_dict(row: ParlourMilkFlowRow) -> dict[str, Any]:
    return {
        "milking_point": row.milking_point,
        "start_seconds": row.start_seconds,
        "duration_seconds": row.duration_seconds,
        "lag_phase_seconds": row.lag_phase_seconds,
        "yield_kg": row.yield_kg,
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
        yield_values=[r.yield_kg for r in rows],
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


def _attach_milking_point_annotations(
    days: dict[tuple[str, dt.date], dict],
    point_map: dict[tuple[str, dt.date, str], list[dict[str, Any]]],
    *,
    include_milking_points: bool,
) -> None:
    farms_seen = {key[0] for key in point_map}
    for farm_key in farms_seen:
        farm_points: dict[tuple[dt.date, str], list[dict[str, Any]]] = {
            (d, s): pts for (f, d, s), pts in point_map.items() if f == farm_key
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


# Keep farm-metric charts useful without loading unbounded history (Render 512MB).
MAX_SHIFT_SUMMARY_SPAN_DAYS = 45
MAX_PEN_BREAKDOWN_SPAN_DAYS = 7
_SHIFT_SUMMARY_YIELD_PER = 2000


def resolve_shift_summary_dates(
    date_from: dt.date | None,
    date_to: dt.date | None,
    *,
    include_pens: bool = False,
) -> tuple[dt.date, dt.date]:
    """Apply defaults and enforce span caps. Raises ValueError on bad ranges."""
    today = dt.date.today()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - dt.timedelta(days=MAX_SHIFT_SUMMARY_SPAN_DAYS)
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    span_days = (date_to - date_from).days
    if include_pens and span_days > MAX_PEN_BREAKDOWN_SPAN_DAYS:
        raise ValueError(
            f"Pen breakdown date range cannot exceed {MAX_PEN_BREAKDOWN_SPAN_DAYS} days."
        )
    if span_days > MAX_SHIFT_SUMMARY_SPAN_DAYS:
        raise ValueError(
            f"Shift summary date range cannot exceed {MAX_SHIFT_SUMMARY_SPAN_DAYS} days."
        )
    return date_from, date_to


def _slim_cow_from_row(
    start_s: int | None,
    dur_s: int | None,
    lag_s: int | None,
    milking_point: int | None,
    yield_kg: float | None,
    flow_15s: float | None,
    flow_30s: float | None,
    flow_60s: float | None,
    flow_120s: float | None,
    pct_2_minutes: float | None,
    milk_yield_2_minutes: float | None,
    flow_rate_at_removal: float | None,
    average_flow: float | None,
    peak_flow: float | None,
) -> dict[str, Any]:
    return {
        "start_seconds": start_s,
        "duration_seconds": dur_s,
        "lag_phase_seconds": lag_s,
        "milking_point": milking_point,
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


def _append_shift_to_days(
    days: dict[tuple[str, dt.date], dict],
    *,
    farm_key: str,
    milking_date: dt.date,
    shift_name: str,
    summary: dict[str, Any],
) -> None:
    day_key = (farm_key, milking_date)
    if day_key not in days:
        days[day_key] = {
            "farm": farm_key,
            "milking_date": milking_date.isoformat(),
            "shifts": [],
            "total_yield_kg": 0.0,
            "total_cows": 0,
        }
    days[day_key]["shifts"].append({"shift": shift_name, **summary})
    days[day_key]["total_yield_kg"] = round(
        days[day_key]["total_yield_kg"] + summary["yield_kg"], 1
    )
    days[day_key]["total_cows"] += summary["cow_count"]


def _finalize_slim_shift(
    days: dict[tuple[str, dt.date], dict],
    point_map: dict[tuple[str, dt.date, str], list[dict[str, Any]]],
    *,
    need_points: bool,
    farm_key: str,
    milking_date: dt.date,
    shift_name: str,
    cows: list[dict[str, Any]],
) -> None:
    pairs = [
        (c["start_seconds"], c["duration_seconds"])
        for c in cows
        if c["start_seconds"] is not None
    ]
    summary = _shift_summary_core(
        yield_kg=sum(c["yield_kg"] or 0.0 for c in cows),
        cow_count=len(cows),
        start_duration_pairs=pairs,
        cows=cows,
        yield_values=[c.get("yield_kg") for c in cows],
    )
    _append_shift_to_days(
        days,
        farm_key=farm_key,
        milking_date=milking_date,
        shift_name=shift_name,
        summary=summary,
    )
    if need_points:
        point_map[(farm_key, milking_date, shift_name)] = (
            _milking_point_summary_from_cows(cows)
        )


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
    date_from, date_to = resolve_shift_summary_dates(
        date_from, date_to, include_pens=include_pens
    )
    need_points = include_milking_points or include_problem_stalls
    # Pen breakdown still uses ORM (cohort correction). Slim column streaming
    # keeps peak memory to ~one shift of cows (Render 512MB).
    need_orm = include_pens
    # Need prior calendar day so Morning can compare to previous Night.
    requested_from = date_from
    query_from = (
        date_from - dt.timedelta(days=1) if need_points else date_from
    )
    # Keep all shifts when annotating problems so "previous shift" exists.
    query_shift = None if need_points else shift

    days: dict[tuple[str, dt.date], dict] = {}
    point_map: dict[tuple[str, dt.date, str], list[dict[str, Any]]] = {}

    if not need_orm:
        stmt = (
            select(
                ParlourMilkFlowRow.farm,
                ParlourMilkFlowRow.milking_date,
                ParlourMilkFlowRow.shift,
                ParlourMilkFlowRow.start_seconds,
                ParlourMilkFlowRow.duration_seconds,
                ParlourMilkFlowRow.lag_phase_seconds,
                ParlourMilkFlowRow.milking_point,
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
            .where(ParlourMilkFlowRow.milking_date >= query_from)
            .where(ParlourMilkFlowRow.milking_date <= date_to)
            .order_by(
                ParlourMilkFlowRow.farm,
                ParlourMilkFlowRow.milking_date,
                ParlourMilkFlowRow.shift,
            )
            .execution_options(yield_per=_SHIFT_SUMMARY_YIELD_PER)
        )
        if farm:
            stmt = stmt.where(ParlourMilkFlowRow.farm == farm.upper())
        if query_shift:
            stmt = stmt.where(ParlourMilkFlowRow.shift == query_shift)

        current_key: tuple[str, dt.date, str] | None = None
        current_cows: list[dict[str, Any]] = []
        for row in db.execute(stmt):
            key = (row[0], row[1], row[2])
            if current_key is not None and key != current_key:
                _finalize_slim_shift(
                    days,
                    point_map,
                    need_points=need_points,
                    farm_key=current_key[0],
                    milking_date=current_key[1],
                    shift_name=current_key[2],
                    cows=current_cows,
                )
                current_cows = []
            current_key = key
            current_cows.append(
                _slim_cow_from_row(
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                )
            )
        if current_key is not None:
            _finalize_slim_shift(
                days,
                point_map,
                need_points=need_points,
                farm_key=current_key[0],
                milking_date=current_key[1],
                shift_name=current_key[2],
                cows=current_cows,
            )
    else:
        stmt_orm = (
            select(ParlourMilkFlowRow)
            .where(ParlourMilkFlowRow.milking_date >= query_from)
            .where(ParlourMilkFlowRow.milking_date <= date_to)
            .order_by(
                ParlourMilkFlowRow.farm,
                ParlourMilkFlowRow.milking_date,
                ParlourMilkFlowRow.shift,
            )
            .execution_options(yield_per=_SHIFT_SUMMARY_YIELD_PER)
        )
        if farm:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.farm == farm.upper())
        if query_shift:
            stmt_orm = stmt_orm.where(ParlourMilkFlowRow.shift == query_shift)

        current_orm_key: tuple[str, dt.date, str] | None = None
        current_orm_rows: list[ParlourMilkFlowRow] = []
        for row in db.scalars(stmt_orm):
            key = (row.farm, row.milking_date, row.shift)
            if current_orm_key is not None and key != current_orm_key:
                summary = _shift_summary(
                    current_orm_rows,
                    include_pens=include_pens,
                    include_milking_points=False,
                )
                _append_shift_to_days(
                    days,
                    farm_key=current_orm_key[0],
                    milking_date=current_orm_key[1],
                    shift_name=current_orm_key[2],
                    summary=summary,
                )
                if need_points:
                    point_map[current_orm_key] = _milking_point_summary(
                        current_orm_rows
                    )
                current_orm_rows = []
            current_orm_key = key
            current_orm_rows.append(row)
        if current_orm_key is not None:
            summary = _shift_summary(
                current_orm_rows,
                include_pens=include_pens,
                include_milking_points=False,
            )
            _append_shift_to_days(
                days,
                farm_key=current_orm_key[0],
                milking_date=current_orm_key[1],
                shift_name=current_orm_key[2],
                summary=summary,
            )
            if need_points:
                point_map[current_orm_key] = _milking_point_summary(current_orm_rows)

    if need_points:
        _attach_milking_point_annotations(
            days,
            point_map,
            include_milking_points=include_milking_points,
        )

    # Sort shifts within each day: Morning → Day → Night (legacy Evening/Afternoon kept)
    shift_order = {
        "Morning": 0,
        "Day": 1,
        "Afternoon": 2,
        "Evening": 3,
        "Night": 4,
    }
    day_list = [
        d
        for d in days.values()
        if dt.date.fromisoformat(d["milking_date"]) >= requested_from
    ]
    day_list.sort(key=lambda d: d["milking_date"], reverse=True)
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
        "yield_kg",
        "cow_count",
        "avg_yield_kg",
        "cows_per_hour",
        "median_rotation_minutes",
        "attachment_idle_seconds",
        "high_flow_takeoff_pct",
        "bimodal_pct",
        "duration_seconds",
        "median_milking_duration_seconds",
        "avg_milking_duration_seconds",
        "median_lag_phase_seconds",
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

_TREND_SHIFT_ORDER = ("Morning", "Day", "Afternoon", "Evening", "Night")


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
        "avg_yield_kg": _avg_yield_kg([row.yield_kg for row in rows]),
        "duration_seconds": span,
        "cows_per_hour": _cows_per_hour(cow_count, span),
    }
    values.update(
        _attachment_idle_fields_from_clock([row.start_seconds for row in rows])
    )
    values.update(
        rotation_stats_from_point_starts(
            [(row.milking_point, row.start_seconds) for row in rows]
        )
    )
    values.update(_cow_quality_stats([_row_to_cow_dict(row) for row in rows]))
    raw = values.get(metric)
    if raw is None:
        return None
    return float(raw)


def _pen_group_metric_value(
    pen_items: list[tuple[ParlourMilkFlowRow, int]],
    metric: str,
) -> float | None:
    """Match pen-summary metrics (cohort pens + gap-split session span)."""
    if not pen_items or metric not in TREND_METRIC_KEYS:
        return None
    slim = [
        {
            "abs_start": abs_start,
            "start_seconds": row.start_seconds,
            "duration_seconds": row.duration_seconds,
            "lag_phase_seconds": row.lag_phase_seconds,
            "milking_point": row.milking_point,
            "yield_kg": row.yield_kg,
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
        for row, abs_start in pen_items
    ]
    return _pen_metric_from_slim(slim, metric)


def _pen_metric_from_slim(items: list[dict[str, Any]], metric: str) -> float | None:
    """Compute a single pen metric without building full quality aggregates."""
    if not items or metric not in TREND_METRIC_KEYS:
        return None
    cow_count = len(items)

    if metric == "cow_count":
        return float(cow_count)

    if metric in {"duration_seconds", "cows_per_hour"}:
        pairs = [
            (int(it["abs_start"]), int(it.get("duration_seconds") or 0))
            for it in items
        ]
        _first, _last, span, _sessions = pen_session_span_seconds(pairs)
        if metric == "duration_seconds":
            return float(span) if span is not None else None
        value = _cows_per_hour(cow_count, span)
        return float(value) if value is not None else None

    if metric == "yield_kg":
        return round(sum(float(it.get("yield_kg") or 0.0) for it in items), 1)

    if metric == "avg_yield_kg":
        value = _avg_yield_kg([it.get("yield_kg") for it in items])
        return float(value) if value is not None else None

    if metric == "median_rotation_minutes":
        stats = rotation_stats_from_point_starts(
            [(it.get("milking_point"), it.get("start_seconds")) for it in items]
        )
        value = stats.get(metric)
        return float(value) if value is not None else None

    if metric == "attachment_idle_seconds":
        if items and items[0].get("abs_start") is not None:
            idle = _attachment_idle_fields_from_abs(
                [int(it["abs_start"]) for it in items if it.get("abs_start") is not None]
            )
        else:
            idle = _attachment_idle_fields_from_clock(
                [it.get("start_seconds") for it in items]
            )
        value = idle.get(metric)
        return float(value) if value is not None else None

    # Flow / takeoff / bi-modal / unit-on / lag — only the requested field.
    if metric == "median_lag_phase_seconds":
        values = [
            float(v)
            for v in (it.get("lag_phase_seconds") for it in items)
            if v is not None
        ]
        med = _median(values, digits=0)
        return float(med) if med is not None else None

    if metric == "median_milking_duration_seconds":
        values = [
            v
            for v in (
                eligible_duration_seconds(it.get("duration_seconds")) for it in items
            )
            if v is not None
        ]
        med = _median(values, digits=0)
        return float(med) if med is not None else None

    if metric == "avg_milking_duration_seconds":
        values = [
            v
            for v in (
                eligible_duration_seconds(it.get("duration_seconds")) for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=0)
        return float(avg) if avg is not None else None

    if metric == "avg_flow_15s":
        values = [
            v
            for v in (
                eligible_interval_flow(
                    it.get("flow_15s"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_flow_30s":
        values = [
            v
            for v in (
                eligible_interval_flow(
                    it.get("flow_30s"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_flow_60s":
        values = [
            v
            for v in (
                eligible_interval_flow(
                    it.get("flow_60s"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_flow_120s":
        values = [
            v
            for v in (
                eligible_interval_flow(
                    it.get("flow_120s"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_pct_2_minutes":
        values = [
            v
            for v in (eligible_pct_2_minutes(it.get("pct_2_minutes")) for it in items)
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_milk_yield_2_minutes":
        values = [
            v
            for v in (
                eligible_milk_yield_2_minutes(it.get("milk_yield_2_minutes"))
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=2)
        return float(avg) if avg is not None else None

    if metric == "avg_flow_rate_at_removal":
        values = [
            v
            for v in (
                eligible_takeoff_flow(
                    it.get("flow_rate_at_removal"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=1)
        return float(avg) if avg is not None else None

    if metric == "avg_average_flow":
        values = [
            v
            for v in (
                eligible_average_flow(
                    it.get("average_flow"),
                    it.get("peak_flow"),
                    yield_kg=it.get("yield_kg"),
                )
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=2)
        return float(avg) if avg is not None else None

    if metric == "avg_peak_flow":
        values = [
            v
            for v in (
                eligible_peak_flow(it.get("peak_flow"), yield_kg=it.get("yield_kg"))
                for it in items
            )
            if v is not None
        ]
        avg = _mean(values, digits=2)
        return float(avg) if avg is not None else None

    if metric == "high_flow_takeoff_pct":
        removal = [
            v
            for v in (
                eligible_takeoff_flow(
                    it.get("flow_rate_at_removal"), yield_kg=it.get("yield_kg")
                )
                for it in items
            )
            if v is not None
        ]
        high_n = sum(1 for v in removal if v > HIGH_FLOW_TAKEOFF_THRESHOLD)
        value = _pct(high_n, len(removal))
        return float(value) if value is not None else None

    if metric == "bimodal_pct":
        flags = [
            is_bimodal(
                it.get("flow_15s"),
                it.get("flow_30s"),
                it.get("flow_60s"),
                yield_kg=it.get("yield_kg"),
            )
            for it in items
        ]
        known = [f for f in flags if f is not None]
        value = _pct(sum(1 for f in known if f), len(known))
        return float(value) if value is not None else None

    return None


def _series_from_shift_points(
    by_shift: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for shift_name in _TREND_SHIFT_ORDER:
        points = by_shift.get(shift_name) or []
        if not points:
            continue
        series.append({"shift": shift_name, "points": points})
    for shift_name, points in sorted(by_shift.items()):
        if shift_name in _TREND_SHIFT_ORDER or not points:
            continue
        series.append({"shift": shift_name, "points": points})
    return series


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

    by_shift: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _TREND_SHIFT_ORDER
    }
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
    return {
        "farm": farm_key,
        "milking_point": milking_point,
        "metric": metric,
        "dates": date_list,
        "series": _series_from_shift_points(by_shift),
        "point_count": len(date_list),
    }


_PEN_TREND_CORE_METRICS = frozenset(
    {
        "cow_count",
        "yield_kg",
        "avg_yield_kg",
        "duration_seconds",
        "cows_per_hour",
        "median_rotation_minutes",
        "attachment_idle_seconds",
        "median_milking_duration_seconds",
        "avg_milking_duration_seconds",
    }
)


def _pen_trend_select_columns(metric: str) -> list[Any]:
    """Columns needed for cohort correction + the requested metric."""
    cols: list[Any] = [
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.shift,
        ParlourMilkFlowRow.pen,
        ParlourMilkFlowRow.start_seconds,
        ParlourMilkFlowRow.duration_seconds,
        ParlourMilkFlowRow.milking_point,
        ParlourMilkFlowRow.yield_kg,
    ]
    if metric in _PEN_TREND_CORE_METRICS:
        return cols
    # Quality / flow metrics — pull only the fields that metric uses.
    if metric == "median_lag_phase_seconds":
        cols.append(ParlourMilkFlowRow.lag_phase_seconds)
        return cols
    if metric in {"avg_flow_15s", "bimodal_pct"}:
        cols.append(ParlourMilkFlowRow.flow_15s)
    if metric in {"avg_flow_30s", "bimodal_pct"}:
        cols.append(ParlourMilkFlowRow.flow_30s)
    if metric in {"avg_flow_60s", "bimodal_pct"}:
        cols.append(ParlourMilkFlowRow.flow_60s)
    if metric == "avg_flow_120s":
        cols.append(ParlourMilkFlowRow.flow_120s)
    if metric == "avg_pct_2_minutes":
        cols.append(ParlourMilkFlowRow.pct_2_minutes)
    if metric == "avg_milk_yield_2_minutes":
        cols.append(ParlourMilkFlowRow.milk_yield_2_minutes)
    if metric in {"avg_flow_rate_at_removal", "high_flow_takeoff_pct"}:
        cols.append(ParlourMilkFlowRow.flow_rate_at_removal)
    if metric in {"avg_average_flow", "avg_peak_flow"}:
        cols.append(ParlourMilkFlowRow.average_flow)
        cols.append(ParlourMilkFlowRow.peak_flow)
    return cols


def pen_metric_trend(
    db: Session,
    *,
    farm: str,
    pen: int | None,
    metric: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Trend of one metric for one pen (cohort-corrected), split by shift over time."""
    if metric not in TREND_METRIC_KEYS:
        raise ValueError(f"Unsupported metric: {metric}")

    farm_key = farm.upper()
    # Slim column load (not full ORM rows). Whole shifts still needed for cohort correction.
    cols = _pen_trend_select_columns(metric)
    col_keys = [c.key for c in cols]
    stmt = select(*cols).where(ParlourMilkFlowRow.farm == farm_key)
    if date_from:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date >= date_from)
    if date_to:
        stmt = stmt.where(ParlourMilkFlowRow.milking_date <= date_to)

    by_session: dict[tuple[dt.date, str], list[dict[str, Any]]] = defaultdict(list)
    for row in db.execute(stmt).all():
        mapping = {key: value for key, value in zip(col_keys, row)}
        by_session[(mapping["milking_date"], mapping["shift"])].append(mapping)

    by_shift: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _TREND_SHIFT_ORDER
    }
    dates: set[str] = set()

    for (milking_date, shift_name), session_rows in sorted(by_session.items()):
        starts = [
            int(r["start_seconds"])
            for r in session_rows
            if r.get("start_seconds") is not None
        ]
        origin = shift_timeline_origin(starts)
        recorded_pens = [r.get("pen") for r in session_rows]
        abs_starts: list[int | None] = []
        for row in session_rows:
            start_s = row.get("start_seconds")
            if start_s is None or origin is None:
                abs_starts.append(None)
            else:
                abs_starts.append(to_absolute_start(int(start_s), origin))

        correctable_idx = [i for i, a in enumerate(abs_starts) if a is not None]
        corrected = list(recorded_pens)
        if correctable_idx:
            sub_pens = [recorded_pens[i] for i in correctable_idx]
            sub_abs = [abs_starts[i] for i in correctable_idx]
            sub_corrected, _ = correct_pens_by_milking_cohort(
                sub_pens, sub_abs  # type: ignore[arg-type]
            )
            for i, new_pen in zip(correctable_idx, sub_corrected):
                corrected[i] = new_pen

        pen_items: list[dict[str, Any]] = []
        for row, abs_start, cpen in zip(session_rows, abs_starts, corrected):
            if abs_start is None:
                continue
            if pen is None:
                if cpen is not None:
                    continue
            elif cpen != pen:
                continue
            item = dict(row)
            item["abs_start"] = abs_start
            pen_items.append(item)

        if not pen_items:
            continue

        value = _pen_metric_from_slim(pen_items, metric)
        date_iso = milking_date.isoformat()
        dates.add(date_iso)
        if shift_name not in by_shift:
            by_shift[shift_name] = []
        by_shift[shift_name].append(
            {
                "date": date_iso,
                "value": value,
                "cow_count": len(pen_items),
            }
        )

    date_list = sorted(dates)
    return {
        "farm": farm_key,
        "pen": pen,
        "metric": metric,
        "dates": date_list,
        "series": _series_from_shift_points(by_shift),
        "point_count": len(date_list),
    }


MAX_STALL_DETAIL_SPAN_DAYS = 4
STALL_DETAIL_SHIFTS = ("Morning", "Day", "Night")
STALL_DETAIL_METRIC_KEYS = (
    "yield_kg",
    "avg_yield_kg",
    "cow_count",
    "high_flow_takeoff_pct",
    "avg_flow_rate_at_removal",
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
)


def _mean_sd(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "sd": None}
    mean = float(statistics.fmean(values))
    sd = float(statistics.stdev(values)) if len(values) >= 2 else None
    return {"mean": mean, "sd": sd}


def _resolve_stall_issue_dates(
    db: Session,
    *,
    farm_key: str,
    date_from: dt.date | None,
    date_to: dt.date | None,
) -> tuple[dt.date, dt.date] | None:
    if date_to is None:
        date_to = db.scalar(
            select(func.max(ParlourMilkFlowRow.milking_date)).where(
                ParlourMilkFlowRow.farm == farm_key
            )
        )
    if date_to is None:
        return None
    if date_from is None:
        date_from = date_to - dt.timedelta(days=3)
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    return date_from, date_to


def _annotated_stall_shifts(
    db: Session,
    *,
    farm_key: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict[tuple[dt.date, str], list[dict[str, Any]]]:
    """Milking-point summaries per shift, with alert/problem flags.

    Loads one extra calendar day so Morning can compare to the previous Night.
    """
    query_from = date_from - dt.timedelta(days=1)

    stmt = select(
        ParlourMilkFlowRow.milking_date,
        ParlourMilkFlowRow.shift,
        ParlourMilkFlowRow.start_seconds,
        ParlourMilkFlowRow.duration_seconds,
        ParlourMilkFlowRow.lag_phase_seconds,
        ParlourMilkFlowRow.milking_point,
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
    ).where(
        ParlourMilkFlowRow.farm == farm_key,
        ParlourMilkFlowRow.milking_date >= query_from,
        ParlourMilkFlowRow.milking_date <= date_to,
    )

    grouped: dict[tuple[dt.date, str], list[dict[str, Any]]] = defaultdict(list)
    for (
        milking_date,
        shift_name,
        start_s,
        dur_s,
        lag_s,
        milking_point,
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
        grouped[(milking_date, shift_name)].append(
            {
                "start_seconds": start_s,
                "duration_seconds": dur_s,
                "lag_phase_seconds": lag_s,
                "milking_point": milking_point,
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

    by_shift: dict[tuple[dt.date, str], list[dict[str, Any]]] = {
        key: _milking_point_summary_from_cows(cows) for key, cows in grouped.items()
    }
    _annotate_milking_point_outliers(by_shift)
    return by_shift


def list_stall_issues(
    db: Session,
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Matrix of milking points × days: count of shifts each stall was a problem.

    Problem = ≥2 SD alert this shift and the same metric alerted on the
    previous shift.
    """
    farm_key = farm.upper()
    resolved = _resolve_stall_issue_dates(
        db, farm_key=farm_key, date_from=date_from, date_to=date_to
    )
    if resolved is None:
        return {
            "farm": farm_key,
            "date_from": None,
            "date_to": None,
            "dates": [],
            "rows": [],
        }
    date_from, date_to = resolved

    by_shift = _annotated_stall_shifts(
        db, farm_key=farm_key, date_from=date_from, date_to=date_to
    )

    dates = [
        date_from + dt.timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]
    date_isos = [d.isoformat() for d in dates]

    # milking_point -> date_iso -> problem shift count
    counts: dict[Any, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    points_seen: set[Any] = set()

    for (milking_date, _shift), points in by_shift.items():
        if milking_date < date_from or milking_date > date_to:
            continue
        date_iso = milking_date.isoformat()
        for point in points:
            point_id = point.get("milking_point")
            if point_id is None:
                continue
            points_seen.add(point_id)
            flags = point.get("outlier_flags") or {}
            if any(flag == "problem" for flag in flags.values()):
                counts[point_id][date_iso] += 1

    rows: list[dict[str, Any]] = []
    for point_id in sorted(points_seen, key=lambda p: (p is None, p if p is not None else 0)):
        by_date = {d: int(counts[point_id].get(d, 0)) for d in date_isos}
        rows.append(
            {
                "milking_point": point_id,
                "by_date": by_date,
                "total": sum(by_date.values()),
            }
        )

    return {
        "farm": farm_key,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "dates": date_isos,
        "rows": rows,
    }


def list_stall_metric_history(
    db: Session,
    *,
    farm: str,
    milking_point: int,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Per-shift metrics and outlier flags for one stall over a short window."""
    farm_key = farm.upper()
    resolved = _resolve_stall_issue_dates(
        db, farm_key=farm_key, date_from=date_from, date_to=date_to
    )
    if resolved is None:
        return {
            "farm": farm_key,
            "milking_point": milking_point,
            "date_from": None,
            "date_to": None,
            "dates": [],
            "shifts": list(STALL_DETAIL_SHIFTS),
            "cells": {},
            "stats": {},
        }
    date_from, date_to = resolved
    if (date_to - date_from).days > MAX_STALL_DETAIL_SPAN_DAYS - 1:
        raise ValueError(
            f"Stall detail date range cannot exceed {MAX_STALL_DETAIL_SPAN_DAYS} days."
        )

    by_shift = _annotated_stall_shifts(
        db, farm_key=farm_key, date_from=date_from, date_to=date_to
    )

    dates = [
        date_from + dt.timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]
    date_isos = [d.isoformat() for d in dates]
    cells: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in date_isos}
    values_by_metric: dict[str, list[float]] = defaultdict(list)

    for (milking_date, shift_name), points in by_shift.items():
        if milking_date < date_from or milking_date > date_to:
            continue
        if shift_name not in STALL_DETAIL_SHIFTS:
            continue
        for point in points:
            for metric in STALL_DETAIL_METRIC_KEYS:
                raw = point.get(metric)
                if raw is None:
                    continue
                try:
                    values_by_metric[metric].append(float(raw))
                except (TypeError, ValueError):
                    continue
        match = next(
            (p for p in points if p.get("milking_point") == milking_point),
            None,
        )
        if match is None:
            continue
        cells[milking_date.isoformat()][shift_name] = match

    stats = {
        metric: _mean_sd(values_by_metric.get(metric, []))
        for metric in STALL_DETAIL_METRIC_KEYS
    }

    return {
        "farm": farm_key,
        "milking_point": milking_point,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "dates": date_isos,
        "shifts": list(STALL_DETAIL_SHIFTS),
        "cells": cells,
        "stats": stats,
    }
