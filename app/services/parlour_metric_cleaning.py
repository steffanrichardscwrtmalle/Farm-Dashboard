"""Per-metric eligibility filters for parlour milk-flow aggregates and scatters.

Exclude bad sensor values from that metric's mean / % only — do not drop the
whole cow from yield / cows / cows-per-hour KPIs.
"""

from __future__ import annotations

from typing import Any

# Incomplete / kick-off attachments — exclude from Unit On Time averages.
MIN_DURATION_SECONDS = 60

# Physicality cap for average flow (kg/min-scale sensor units).
MAX_AVERAGE_FLOW = 10.0

# Ghost / incomplete milkings — exclude from flow-rate metrics (kg ≈ L for milk).
MIN_YIELD_KG_FOR_FLOW_METRICS = 2.0

# Scatter / aggregate keys that use the yield floor above.
FLOW_RATE_METRICS = frozenset(
    {
        "flow_15s",
        "flow_30s",
        "flow_60s",
        "flow_120s",
        "average_flow",
        "peak_flow",
        "flow_rate_at_removal",
    }
)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def eligible_yield_for_flow_metrics(yield_kg: Any) -> bool:
    """True when milking yield is high enough to include in flow-rate stats."""
    y = _as_float(yield_kg)
    return y is not None and y >= MIN_YIELD_KG_FOR_FLOW_METRICS


def eligible_positive(value: Any) -> float | None:
    """Return value if present and > 0, else None."""
    v = _as_float(value)
    if v is None or v <= 0:
        return None
    return v


def eligible_interval_flow(value: Any, *, yield_kg: Any = None) -> float | None:
    """15s / 30s / 60s / 120s flow: exclude zeros, missing, and yield < 2 kg."""
    if not eligible_yield_for_flow_metrics(yield_kg):
        return None
    return eligible_positive(value)


def eligible_duration_seconds(value: Any) -> float | None:
    """Unit On Time: exclude incomplete milkings under 60s."""
    v = _as_float(value)
    if v is None or v < MIN_DURATION_SECONDS:
        return None
    return v


def eligible_average_flow(
    average_flow: Any,
    peak_flow: Any = None,
    *,
    yield_kg: Any = None,
) -> float | None:
    """Average flow: > 0, <= peak when peak present, <= hard cap, yield >= 2 kg."""
    if not eligible_yield_for_flow_metrics(yield_kg):
        return None
    avg = _as_float(average_flow)
    if avg is None or avg <= 0 or avg > MAX_AVERAGE_FLOW:
        return None
    peak = _as_float(peak_flow)
    if peak is not None and avg > peak:
        return None
    return avg


def eligible_peak_flow(value: Any, *, yield_kg: Any = None) -> float | None:
    if not eligible_yield_for_flow_metrics(yield_kg):
        return None
    return eligible_positive(value)


def eligible_yield_kg(value: Any) -> float | None:
    return eligible_positive(value)


def eligible_milk_yield_2_minutes(value: Any) -> float | None:
    return eligible_positive(value)


def eligible_takeoff_flow(value: Any, *, yield_kg: Any = None) -> float | None:
    """Takeoff / flow at removal: exclude zeros and yield < 2 kg."""
    if not eligible_yield_for_flow_metrics(yield_kg):
        return None
    return eligible_positive(value)


def eligible_pct_2_minutes(value: Any) -> float | None:
    """% in 2 minutes: keep 0–100; exclude nulls only."""
    return _as_float(value)


def is_bimodal_eligible(
    flow_15s: Any,
    flow_30s: Any,
    flow_60s: Any,
    *,
    yield_kg: Any = None,
) -> bool:
    """Bi-modal scoring needs yield >= 2 kg and all three early flows > 0."""
    return (
        eligible_yield_for_flow_metrics(yield_kg)
        and eligible_positive(flow_15s) is not None
        and eligible_positive(flow_30s) is not None
        and eligible_positive(flow_60s) is not None
    )


def is_bimodal(
    flow_15s: Any,
    flow_30s: Any,
    flow_60s: Any,
    *,
    yield_kg: Any = None,
) -> bool | None:
    """Bi-modal let-down: 30s < 15s, or 60s < 15s, or 60s < 30s.

    Returns None when the cow is not eligible for bi-modal scoring.
    """
    if not is_bimodal_eligible(
        flow_15s, flow_30s, flow_60s, yield_kg=yield_kg
    ):
        return None
    f15 = float(flow_15s)
    f30 = float(flow_30s)
    f60 = float(flow_60s)
    return f30 < f15 or f60 < f15 or f60 < f30


def scatter_metric_value(
    metric: str,
    value: Any,
    *,
    peak_flow: Any = None,
    yield_kg: Any = None,
) -> float | None:
    """Apply the same per-metric filters used in aggregates for scatter Y values."""
    if metric in {"flow_15s", "flow_30s", "flow_60s", "flow_120s"}:
        return eligible_interval_flow(value, yield_kg=yield_kg)
    if metric == "duration_seconds":
        return eligible_duration_seconds(value)
    if metric == "average_flow":
        return eligible_average_flow(value, peak_flow, yield_kg=yield_kg)
    if metric == "peak_flow":
        return eligible_peak_flow(value, yield_kg=yield_kg)
    if metric == "yield_kg":
        return eligible_yield_kg(value)
    if metric == "milk_yield_2_minutes":
        return eligible_milk_yield_2_minutes(value)
    if metric == "flow_rate_at_removal":
        return eligible_takeoff_flow(value, yield_kg=yield_kg)
    if metric == "pct_2_minutes":
        return eligible_pct_2_minutes(value)
    return _as_float(value)
