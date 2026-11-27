"""Serum Total Protein (STP) report for calf events."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, CowEvent
from app.services.events_common import (
    _apply_fiscal_year,
    _cap_slider_max,
    _clamp_date,
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _get_fiscal_year_options,
    normalize_farms,
)

STP_EVENT = "STP"
STP_MAX_AGE_DAYS = 7

STP_BREED_BEEF = "beef"
STP_BREED_DAIRY = "dairy"
STP_BREED_UNKNOWN = "unknown"
STP_BREED_OPTIONS: tuple[str, ...] = (STP_BREED_BEEF, STP_BREED_DAIRY)
STP_DEFAULT_BREED_TYPES: tuple[str, ...] = (STP_BREED_DAIRY,)
STP_BEEF_CBREED_MIN = 102  # Dairy CBRD < 102; beef CBRD >= 102

STP_CATEGORY_EXCELLENT = "excellent"
STP_CATEGORY_GOOD = "good"
STP_CATEGORY_FAIR = "fair"
STP_CATEGORY_POOR = "poor"
STP_CATEGORY_UNKNOWN = "unknown"

STP_CATEGORY_ORDER: tuple[str, ...] = (
    STP_CATEGORY_EXCELLENT,
    STP_CATEGORY_GOOD,
    STP_CATEGORY_FAIR,
    STP_CATEGORY_POOR,
)

STP_CATEGORY_LABELS: dict[str, str] = {
    STP_CATEGORY_EXCELLENT: "Excellent",
    STP_CATEGORY_GOOD: "Good",
    STP_CATEGORY_FAIR: "Fair",
    STP_CATEGORY_POOR: "Poor",
    STP_CATEGORY_UNKNOWN: "Unknown",
}

STP_TARGETS: dict[str, float] = {
    "excellent_min_pct": 40.0,
    "good_or_better_min_pct": 70.0,
    "fair_or_better_min_pct": 90.0,
    "poor_max_pct": 10.0,
}

# Default birth-date window when the client does not pass birth_from/birth_to:
# most recent STP birth date → that date minus this many days (clamped to bounds).
STP_DEFAULT_LOOKBACK_DAYS = 90

_PROTEIN_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_protein_value(*fields: str | None) -> float | None:
    for raw in fields:
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            match = _PROTEIN_VALUE_RE.search(text)
            if not match:
                continue
            value = float(match.group(1))
        if 0 < value < 20:
            return value
    return None


def _classify_protein(value: float | None) -> str:
    if value is None:
        return STP_CATEGORY_UNKNOWN
    if value >= 6.2:
        return STP_CATEGORY_EXCELLENT
    if value >= 5.8:
        return STP_CATEGORY_GOOD
    if value >= 5.1:
        return STP_CATEGORY_FAIR
    return STP_CATEGORY_POOR


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in STP_CATEGORY_ORDER + (STP_CATEGORY_UNKNOWN,)}


def normalize_breed_types(breed_types: list[str] | None) -> list[str]:
    if not breed_types:
        return list(STP_DEFAULT_BREED_TYPES)
    selected = [value.lower() for value in breed_types if value.lower() in STP_BREED_OPTIONS]
    return selected or list(STP_DEFAULT_BREED_TYPES)


def classify_stp_breed(cbrd: int | None) -> str:
    """Dairy CBRD codes are below 102; beef codes are 102 and above."""
    if cbrd is None:
        return STP_BREED_UNKNOWN
    if cbrd < STP_BEEF_CBREED_MIN:
        return STP_BREED_DAIRY
    return STP_BREED_BEEF


def _build_performance(counts: dict[str, int]) -> dict[str, Any]:
    excellent = counts.get(STP_CATEGORY_EXCELLENT, 0)
    good = counts.get(STP_CATEGORY_GOOD, 0)
    fair = counts.get(STP_CATEGORY_FAIR, 0)
    poor = counts.get(STP_CATEGORY_POOR, 0)
    unknown = counts.get(STP_CATEGORY_UNKNOWN, 0)
    total = excellent + good + fair + poor

    if total == 0:
        return {
            "total": 0,
            "unknown": unknown,
            "excellent": {"count": 0, "pct": 0.0},
            "good_or_better": {"count": 0, "pct": 0.0},
            "fair_or_better": {"count": 0, "pct": 0.0},
            "poor": {"count": 0, "pct": 0.0},
            "targets_met": {
                "excellent": False,
                "good_or_better": False,
                "fair_or_better": False,
                "poor": False,
            },
        }

    excellent_pct = round(excellent / total * 100, 1)
    good_or_better_count = excellent + good
    good_or_better_pct = round(good_or_better_count / total * 100, 1)
    fair_or_better_count = excellent + good + fair
    fair_or_better_pct = round(fair_or_better_count / total * 100, 1)
    poor_pct = round(poor / total * 100, 1)

    return {
        "total": total,
        "unknown": unknown,
        "excellent": {"count": excellent, "pct": excellent_pct},
        "good_or_better": {"count": good_or_better_count, "pct": good_or_better_pct},
        "fair_or_better": {"count": fair_or_better_count, "pct": fair_or_better_pct},
        "poor": {"count": poor, "pct": poor_pct},
        "targets_met": {
            "excellent": excellent_pct >= STP_TARGETS["excellent_min_pct"],
            "good_or_better": good_or_better_pct >= STP_TARGETS["good_or_better_min_pct"],
            "fair_or_better": fair_or_better_pct >= STP_TARGETS["fair_or_better_min_pct"],
            "poor": poor_pct < STP_TARGETS["poor_max_pct"],
        },
    }


def _stp_filters():
    age_days = CowEvent.event_date - CowEvent.bdat
    return (
        CowEvent.event == STP_EVENT,
        CowEvent.lact == 0,
        CowEvent.bdat.isnot(None),
        CowEvent.event_date.isnot(None),
        age_days >= 0,
        age_days <= STP_MAX_AGE_DAYS,
    )


def _base_stp_query():
    return select(CowEvent).where(*_stp_filters())


def _farm_block(counts: dict[str, int]) -> dict[str, Any]:
    performance = _build_performance(counts)
    return {
        "counts": {key: counts.get(key, 0) for key in STP_CATEGORY_ORDER},
        "unknown": counts.get(STP_CATEGORY_UNKNOWN, 0),
        "total": performance["total"],
        "performance": performance,
    }


def _parse_fiscal_year(value: str | int | None) -> tuple[int | None, bool]:
    """Return (fiscal_year, any_year). None/invalid with any_year False means default."""
    if value is None:
        return None, False
    if isinstance(value, int):
        return value, False
    text = str(value).strip().lower()
    if text in {"", "any"}:
        return None, True
    try:
        return int(text), False
    except ValueError:
        return None, False


def _default_birth_range(
    latest: dt.date | None,
    slider_min: dt.date,
    slider_max: dt.date,
    *,
    lookback_days: int = STP_DEFAULT_LOOKBACK_DAYS,
) -> tuple[dt.date, dt.date]:
    """Most recent data date → that date minus lookback_days, clamped to slider bounds."""
    if latest is None:
        return slider_min, slider_max
    end = _clamp_date(latest, slider_min, slider_max)
    start = _clamp_date(end - dt.timedelta(days=lookback_days), slider_min, slider_max)
    if start > end:
        return slider_min, slider_max
    return start, end


def build_stp_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    breed_types: list[str] | None = None,
    birth_from: dt.date | None = None,
    birth_to: dt.date | None = None,
    fiscal_year: str | int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_breeds = normalize_breed_types(breed_types)
    today = today or dt.date.today()
    requested_year, any_year = _parse_fiscal_year(fiscal_year)
    option_farms = selected_farms or list(HERD_FARM_OPTIONS)
    fiscal_year_options = _get_fiscal_year_options(db, (STP_EVENT,), option_farms)

    if any_year:
        resolved_year = None
    elif requested_year is not None:
        resolved_year = requested_year
    elif fiscal_year_options:
        resolved_year = fiscal_year_options[0]
    else:
        resolved_year = _fiscal_year_from_date(today)
    if resolved_year is not None and resolved_year not in fiscal_year_options:
        fiscal_year_options = [resolved_year, *fiscal_year_options]

    bounds_query = select(func.min(CowEvent.bdat), func.max(CowEvent.bdat)).where(
        *_stp_filters()
    )
    if option_farms:
        bounds_query = bounds_query.where(CowEvent.farm.in_(option_farms))
    bounds_min, bounds_max = db.execute(bounds_query).one()

    if resolved_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(resolved_year)
    elif bounds_min is not None and bounds_max is not None:
        slider_min, slider_max = bounds_min, bounds_max
    else:
        slider_min, slider_max = _fiscal_year_calendar_bounds(
            _fiscal_year_from_date(today)
        )

    # Cap empty future months only — keep the 90-day default window separately.
    slider_max = _cap_slider_max(
        slider_max,
        data_max=bounds_max,
        today=today,
        allow_future=False,
    )
    if slider_max < slider_min:
        slider_max = slider_min

    birth_bounds = {
        "min": slider_min.isoformat(),
        "max": slider_max.isoformat(),
    }

    latest_in_scope_query = select(func.max(CowEvent.bdat)).where(*_stp_filters())
    if option_farms:
        latest_in_scope_query = latest_in_scope_query.where(
            CowEvent.farm.in_(option_farms)
        )
    latest_in_scope_query = _apply_fiscal_year(latest_in_scope_query, resolved_year)
    latest_in_scope_query = latest_in_scope_query.where(CowEvent.bdat >= slider_min)
    latest_in_scope_query = latest_in_scope_query.where(CowEvent.bdat <= slider_max)
    latest_birth = db.scalar(latest_in_scope_query)

    if birth_from is None and birth_to is None:
        effective_from, effective_to = _default_birth_range(
            latest_birth, slider_min, slider_max
        )
    else:
        effective_from = birth_from if birth_from is not None else slider_min
        effective_to = birth_to if birth_to is not None else slider_max
        effective_from = _clamp_date(effective_from, slider_min, slider_max)
        effective_to = _clamp_date(effective_to, slider_min, slider_max)
        if effective_from > effective_to:
            effective_from, effective_to = effective_to, effective_from

    query = _base_stp_query()
    query = query.where(CowEvent.bdat >= effective_from)
    query = query.where(CowEvent.bdat <= effective_to)
    query = _apply_fiscal_year(query, resolved_year)
    if selected_farms:
        query = query.where(CowEvent.farm.in_(selected_farms))

    rows = list(db.scalars(query).all())

    by_farm: dict[str, dict[str, int]] = {
        farm: _empty_counts() for farm in HERD_FARM_OPTIONS
    }
    total = _empty_counts()

    for row in rows:
        farm = row.farm if row.farm in HERD_FARM_OPTIONS else None
        if farm is None:
            continue
        breed = classify_stp_breed(row.cbrd)
        if breed not in selected_breeds:
            continue
        category = _classify_protein(_parse_protein_value(row.remark, row.r, row.t))
        by_farm[farm][category] += 1
        if farm in selected_farms:
            total[category] += 1

    latest_import = db.scalar(
        select(func.max(CowEvent.import_timestamp)).where(CowEvent.event == STP_EVENT)
    )

    return {
        "latest_import": latest_import.isoformat() if latest_import else None,
        "birth_bounds": birth_bounds,
        "birth_from": effective_from.isoformat(),
        "birth_to": effective_to.isoformat(),
        "latest_birth": latest_birth.isoformat() if latest_birth else None,
        "fiscal_year": resolved_year,
        "fiscal_year_options": fiscal_year_options,
        "selected_farms": selected_farms,
        "selected_breeds": selected_breeds,
        "farms": {farm: _farm_block(by_farm[farm]) for farm in HERD_FARM_OPTIONS},
        "total": _farm_block(total),
        "categories": [
            {"id": key, "label": STP_CATEGORY_LABELS[key]} for key in STP_CATEGORY_ORDER
        ],
        "targets": STP_TARGETS,
        "max_age_days": STP_MAX_AGE_DAYS,
        "default_lookback_days": STP_DEFAULT_LOOKBACK_DAYS,
        "breed_options": [
            {"id": STP_BREED_BEEF, "label": "Beef"},
            {"id": STP_BREED_DAIRY, "label": "Dairy"},
        ],
        "bands": {
            "excellent": "≥ 6.2",
            "good": "≥ 5.8 and < 6.2",
            "fair": "≥ 5.1 and < 5.8",
            "poor": "< 5.1",
        },
    }
