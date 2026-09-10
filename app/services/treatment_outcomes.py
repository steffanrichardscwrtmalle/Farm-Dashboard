"""Respiratory treatment recovery curves from SenseHub health + DairyComp events."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import median
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import CowEvent, HerdInventory, SenseHubYoungstockHealth
from app.services.events_common import (
    DEFAULT_DISEASE_EPISODE_GAP_DAYS,
    _clamp_date,
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _get_date_bounds,
    _get_fiscal_year_options,
    is_loxicom_only_remark,
    normalize_farms,
)
from app.services.herd_import_utils import CATEGORY_BEEF, category_from_birth
from app.services.sensehub_youngstock import (
    LIVE_SLOT,
    _inventory_indexes,
    _scr_id_keys,
    classify_antibiotic,
    etag4,
    health_band,
    match_inventory,
)

DISEASE = "RESP"
WINDOW_BEFORE = 2
WINDOW_AFTER = 14
RECOVERY_THRESHOLD = 85.0
RECOVERY_HOLD_DAYS = 2
RELAPSE_DAYS = 21
DEFAULT_MONTH_SPAN = 3
MIN_DEFAULT_EPISODES = 50
EXIT_EVENTS = frozenset({"SOLD", "DIED"})

OFFSETS: tuple[int, ...] = tuple(range(-WINDOW_BEFORE, WINDOW_AFTER + 1))
PRODUCT_ORDER: tuple[str, ...] = ("draxxin", "fenflor", "other")
PRODUCT_LABELS: dict[str, str] = {
    "draxxin": "Draxxin",
    "fenflor": "Fenflor",
    "other": "Other",
}
STARTING_BANDS: tuple[str, ...] = ("low", "watch", "moderate", "healthy")
STARTING_BAND_LABELS: dict[str, str] = {
    "low": "Low (<80)",
    "watch": "Watch (80–84)",
    "moderate": "Moderate (85–89)",
    "healthy": "Healthy (≥90)",
}
BREED_ORDER: tuple[str, ...] = ("dairy", "beef")
DEFAULT_BREEDS: tuple[str, ...] = ("dairy",)
BREED_LABELS: dict[str, str] = {
    "dairy": "Dairy",
    "beef": "Beef",
}
AGE_BANDS: tuple[str, ...] = ("under_3w", "3_to_8w", "over_8w")
AGE_BAND_LABELS: dict[str, str] = {
    "under_3w": "Under 3 weeks",
    "3_to_8w": "3–8 weeks",
    "over_8w": "Over 8 weeks",
}
_BAND_FROM_HEALTH: dict[str, str] = {
    "red": "low",
    "yellow": "watch",
    "blue": "moderate",
    "green": "healthy",
}


def _event_code(event: CowEvent | str | None) -> str:
    if isinstance(event, CowEvent):
        return str(event.event or "").strip().upper()
    return str(event or "").strip().upper()


def _is_counted_resp(event: CowEvent) -> bool:
    return _event_code(event) == DISEASE and not is_loxicom_only_remark(event.remark)


def episode_start_dates(dates: list[dt.date], *, gap_days: int = DEFAULT_DISEASE_EPISODE_GAP_DAYS) -> list[dt.date]:
    """Keep the first treatment date in each gap-separated cluster."""
    starts: list[dt.date] = []
    last: dt.date | None = None
    for day in sorted(set(dates)):
        if last is None or (day - last).days > gap_days:
            starts.append(day)
            last = day
    return starts


def daily_averages(
    samples: list[SenseHubYoungstockHealth],
    start: dt.date,
) -> dict[int, float]:
    """Mean of permanent (non-live) health-index slots for each day around treatment."""
    by_day: dict[dt.date, list[float]] = defaultdict(list)
    for sample in samples:
        if sample.slot == LIVE_SLOT or sample.sampled_at is None or sample.health_index is None:
            continue
        by_day[sample.sampled_at.date()].append(float(sample.health_index))
    result: dict[int, float] = {}
    for offset in OFFSETS:
        values = by_day.get(start + dt.timedelta(days=offset))
        if values:
            result[offset] = sum(values) / len(values)
    return result


def days_to_recover(daily: dict[int, float]) -> int | None:
    """First day of a two-day stretch at or above the recovery threshold."""
    last_start = WINDOW_AFTER - (RECOVERY_HOLD_DAYS - 1)
    for day in range(0, last_start + 1):
        stretch = [daily.get(day + step) for step in range(RECOVERY_HOLD_DAYS)]
        if all(value is not None and value >= RECOVERY_THRESHOLD for value in stretch):
            return day
    return None


def recovered_by_day(daily: dict[int, float], day: int) -> bool:
    recovered = days_to_recover(daily)
    if recovered is None:
        return False
    return recovered + (RECOVERY_HOLD_DAYS - 1) <= day


def starting_health_value(daily: dict[int, float]) -> float | None:
    if -1 in daily:
        return daily[-1]
    return daily.get(0)


def starting_band_for(value: float | None) -> str | None:
    band = _BAND_FROM_HEALTH.get(health_band(value) or "")
    return band


def age_band_for(age_days: int | None) -> str | None:
    if age_days is None:
        return None
    if age_days < 21:
        return "under_3w"
    if age_days < 56:
        return "3_to_8w"
    return "over_8w"


def parse_fiscal_year(value: str | int | None) -> tuple[int | None, bool]:
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


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _month_end(value: dt.date) -> dt.date:
    if value.month == 12:
        return dt.date(value.year, 12, 31)
    return dt.date(value.year, value.month + 1, 1) - dt.timedelta(days=1)


def _shift_months(value: dt.date, months: int) -> dt.date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, 1)


def default_month_range(
    slider_min: dt.date,
    slider_max: dt.date,
    *,
    today: dt.date,
    months: int = DEFAULT_MONTH_SPAN,
) -> tuple[dt.date, dt.date]:
    """Last `months` calendar months up to today, clamped to the slider bounds."""
    reference = min(today, slider_max)
    end_month = _month_start(reference)
    start_month = _shift_months(end_month, -(months - 1))
    start = max(start_month, slider_min)
    end = min(_month_end(end_month), slider_max)
    if start > end:
        return slider_min, slider_max
    return start, end


def normalize_products(products: list[str] | None) -> list[str] | None:
    if products is None:
        return None
    selected = [item for item in products if item in PRODUCT_ORDER]
    return selected


def event_count_label(number: int) -> str:
    if number % 100 in {11, 12, 13}:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix} treatment"


def breed_for(
    inventory: HerdInventory | None,
    events: list[CowEvent] | None = None,
) -> str:
    """Dairy or beef, using inventory category then CBRD / gender."""
    if inventory is not None:
        category = (inventory.category or "").strip()
        if category:
            return "beef" if category.casefold() == "beef" else "dairy"
        if inventory.cbrd is not None:
            return (
                "beef"
                if category_from_birth(inventory.cbrd, inventory.gender) == CATEGORY_BEEF
                else "dairy"
            )
    for event in events or []:
        if event.cbrd is None:
            continue
        return (
            "beef"
            if category_from_birth(event.cbrd, event.gndr) == CATEGORY_BEEF
            else "dairy"
        )
    return "dairy"


def normalize_breeds(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    selected = []
    for value in values:
        key = str(value or "").strip().casefold()
        if key in BREED_ORDER and key not in selected:
            selected.append(key)
    return selected


def normalize_event_counts(values: list[str] | list[int] | None) -> list[int] | None:
    if values is None:
        return None
    selected: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 1 and number not in selected:
            selected.append(number)
    return selected


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _pct(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(100.0 * part / whole, 1)


def _median(values: list[int | float]) -> float | None:
    if not values:
        return None
    return _round(float(median(values)))


def _resolve_animal_id(
    event: CowEvent,
    health_ids: set[str],
    by_cow: dict[str, HerdInventory],
    by_tag: dict[str, HerdInventory],
) -> str | None:
    candidates: list[str] = []
    for raw in (event.cow_id, event.etag):
        candidates.extend(_scr_id_keys(raw))
    inventory = None
    if event.cow_id:
        inventory = by_cow.get(event.cow_id) or by_tag.get(event.cow_id)
    if inventory is None and event.etag:
        inventory = by_cow.get(event.etag) or by_tag.get(event.etag)
    if inventory is None:
        for raw in (event.cow_id, event.etag):
            inventory = match_inventory(str(raw or ""), by_cow, by_tag)
            if inventory is not None:
                break
    if inventory is not None:
        candidates.extend(_scr_id_keys(inventory.cow_id))
        candidates.extend(_scr_id_keys(inventory.etag))
    for key in candidates:
        if key in health_ids:
            return key
        if len(key) > 6 and key[-6:] in health_ids:
            return key[-6:]
    return None


def _age_at_treatment(
    start: dt.date,
    inventory: HerdInventory | None,
    samples: list[SenseHubYoungstockHealth],
) -> int | None:
    if inventory is not None and inventory.bdat is not None:
        return (start - inventory.bdat).days
    nearest: tuple[int, int] | None = None
    for sample in samples:
        if sample.age_days is None or sample.sampled_at is None:
            continue
        delta = abs((sample.sampled_at.date() - start).days)
        if nearest is None or delta < nearest[0]:
            nearest = (delta, int(sample.age_days) - (sample.sampled_at.date() - start).days)
    if nearest is None:
        return None
    return nearest[1]


def _first_remark(events: list[CowEvent]) -> str | None:
    for event in events:
        text = (event.remark or "").strip()
        if text:
            return text
    for event in events:
        text = (event.protocols or "").strip()
        if text:
            return text
    return None


def _empty_payload(
    *,
    date_from: dt.date,
    date_to: dt.date,
    farms: list[str],
    starting_band: str | None,
    age_band: str | None,
    products: list[str] | None,
    breeds: list[str] | None,
    fiscal_year: int | None,
    fiscal_year_options: list[int],
    date_bounds: dict[str, str] | None,
    product_counts: list[dict[str, Any]] | None = None,
    event_counts: list[int] | None = None,
    event_count_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "disease": DISEASE,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "farms": farms,
        "starting_band": starting_band,
        "age_band": age_band,
        "products": products or [],
        "breeds": breeds or list(DEFAULT_BREEDS),
        "event_counts": event_counts or [],
        "event_count_options": event_count_options or [],
        "fiscal_year": fiscal_year,
        "fiscal_year_options": fiscal_year_options,
        "date_bounds": date_bounds,
        "episode_count": 0,
        "skipped_no_health": 0,
        "curve": {
            "offsets": list(OFFSETS),
            "series": [],
        },
        "summary": [],
        "unrecovered": [],
        "product_counts": product_counts or [
            {"id": key, "label": PRODUCT_LABELS[key], "episodes": 0, "selected": False}
            for key in PRODUCT_ORDER
        ],
        "filters": {
            "starting_bands": [
                {"id": key, "label": STARTING_BAND_LABELS[key]} for key in STARTING_BANDS
            ],
            "age_bands": [
                {"id": key, "label": AGE_BAND_LABELS[key]} for key in AGE_BANDS
            ],
            "products": [
                {"id": key, "label": PRODUCT_LABELS[key]} for key in PRODUCT_ORDER
            ],
            "breeds": [
                {"id": key, "label": BREED_LABELS[key]} for key in BREED_ORDER
            ],
            "event_counts": event_count_options or [],
        },
    }


def _summary_row(product: str, label: str, episodes: list[dict[str, Any]]) -> dict[str, Any]:
    recoveries = [
        episode["days_to_recover"]
        for episode in episodes
        if episode["days_to_recover"] is not None
    ]
    day7 = [episode for episode in episodes if episode["eligible_day7"]]
    day14 = [episode for episode in episodes if episode["eligible_day14"]]
    relapse = [episode for episode in episodes if episode["eligible_relapse"]]
    left = sum(1 for episode in episodes if episode["left_system"])
    recovered7 = sum(1 for episode in day7 if episode["recovered_by_day7"])
    recovered14 = sum(1 for episode in day14 if episode["recovered_by_day14"])
    relapsed = sum(1 for episode in relapse if episode["relapsed"])
    return {
        "product": product,
        "label": label,
        "episodes": len(episodes),
        "median_days_to_recover": _median(recoveries),
        "recovered_by_day_7": recovered7,
        "recovered_by_day_7_eligible": len(day7),
        "recovered_by_day_7_pct": _pct(recovered7, len(day7)),
        "recovered_by_day_14": recovered14,
        "recovered_by_day_14_eligible": len(day14),
        "recovered_by_day_14_pct": _pct(recovered14, len(day14)),
        "relapsed": relapsed,
        "relapsed_eligible": len(relapse),
        "relapsed_pct": _pct(relapsed, len(relapse)),
        "left": left,
        "left_pct": _pct(left, len(episodes)),
    }


def _curve_series(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for product in PRODUCT_ORDER:
        chosen = [episode for episode in episodes if episode["product"] == product]
        if not chosen:
            continue
        means: list[float | None] = []
        counts: list[int] = []
        for offset in OFFSETS:
            values = [
                episode["daily"][offset]
                for episode in chosen
                if episode["daily"].get(offset) is not None
            ]
            counts.append(len(values))
            means.append(_round(sum(values) / len(values)) if values else None)
        series.append(
            {
                "product": product,
                "label": PRODUCT_LABELS[product],
                "means": means,
                "counts": counts,
            }
        )
    return series


def treatment_outcomes(
    db: Session,
    *,
    farms: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    starting_band: str | None = None,
    age_band: str | None = None,
    products: list[str] | None = None,
    breeds: list[str] | None = None,
    event_counts: list[str] | list[int] | None = None,
    fiscal_year: str | int | None = None,
    min_episodes: int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    today = today or dt.date.today()
    selected_farms = normalize_farms(farms)
    if starting_band and starting_band not in STARTING_BANDS:
        starting_band = None
    if age_band and age_band not in AGE_BANDS:
        age_band = None
    selected_products = normalize_products(products)
    selected_breeds = normalize_breeds(breeds)
    if selected_breeds is None:
        selected_breeds = list(DEFAULT_BREEDS)
    selected_event_counts = normalize_event_counts(event_counts)
    requested_year, any_year = parse_fiscal_year(fiscal_year)
    fiscal_year_options = (
        _get_fiscal_year_options(db, (DISEASE,), selected_farms) if selected_farms else []
    )
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

    bounds_min, bounds_max = (
        _get_date_bounds(db, (DISEASE,), selected_farms, today=today)
        if selected_farms
        else (None, None)
    )
    if resolved_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(resolved_year)
    elif bounds_min is not None and bounds_max is not None:
        slider_min, slider_max = bounds_min, bounds_max
    else:
        slider_min, slider_max = default_month_range(
            dt.date(today.year - 1, 4, 1),
            today,
            today=today,
        )
    date_bounds = {"min": slider_min.isoformat(), "max": slider_max.isoformat()}
    if date_from is None or date_to is None:
        start, end = default_month_range(slider_min, slider_max, today=today)
    else:
        start, end = date_from, date_to
    if start > end:
        start, end = end, start
    start = _clamp_date(start, slider_min, slider_max)
    end = _clamp_date(end, slider_min, slider_max)

    empty = _empty_payload(
        date_from=start,
        date_to=end,
        farms=selected_farms,
        starting_band=starting_band,
        age_band=age_band,
        products=selected_products,
        breeds=selected_breeds,
        fiscal_year=resolved_year,
        fiscal_year_options=fiscal_year_options,
        date_bounds=date_bounds,
    )
    if not selected_farms:
        return empty

    health_ids = set(db.scalars(select(SenseHubYoungstockHealth.animal_id).distinct()).all())
    if not health_ids:
        return empty

    lookback = start - dt.timedelta(days=DEFAULT_DISEASE_EPISODE_GAP_DAYS)
    event_horizon = end + dt.timedelta(days=RELAPSE_DAYS)
    health_from = start - dt.timedelta(days=WINDOW_BEFORE)
    health_to = end + dt.timedelta(days=WINDOW_AFTER)

    events = list(
        db.scalars(
            select(CowEvent).where(
                CowEvent.farm.in_(selected_farms),
                CowEvent.event_date.is_not(None),
                CowEvent.event_date <= event_horizon,
                or_(
                    CowEvent.event == DISEASE,
                    and_(
                        CowEvent.event.in_(tuple(EXIT_EVENTS)),
                        CowEvent.event_date >= lookback,
                    ),
                ),
            )
        ).all()
    )
    if not events:
        return empty

    by_cow, by_tag = _inventory_indexes(db)
    by_animal_resp: dict[str, list[CowEvent]] = defaultdict(list)
    by_animal_exit: dict[str, list[CowEvent]] = defaultdict(list)
    matched_ids: set[str] = set()
    for event in events:
        animal_id = _resolve_animal_id(event, health_ids, by_cow, by_tag)
        if animal_id is None:
            continue
        matched_ids.add(animal_id)
        if _is_counted_resp(event):
            by_animal_resp[animal_id].append(event)
        elif _event_code(event) in EXIT_EVENTS:
            by_animal_exit[animal_id].append(event)

    if not by_animal_resp:
        return empty

    samples_by_animal: dict[str, list[SenseHubYoungstockHealth]] = defaultdict(list)
    sample_rows = db.scalars(
        select(SenseHubYoungstockHealth).where(
            SenseHubYoungstockHealth.animal_id.in_(matched_ids),
            SenseHubYoungstockHealth.sampled_at >= dt.datetime.combine(health_from, dt.time.min),
            SenseHubYoungstockHealth.sampled_at
            < dt.datetime.combine(health_to + dt.timedelta(days=1), dt.time.min),
        )
    ).all()
    for sample in sample_rows:
        samples_by_animal[sample.animal_id].append(sample)

    episodes: list[dict[str, Any]] = []
    skipped_no_health = 0
    for animal_id, resp_events in by_animal_resp.items():
        inventory = match_inventory(animal_id, by_cow, by_tag)
        farm = next((event.farm for event in resp_events if event.farm), None)
        if farm is None and inventory is not None:
            farm = inventory.farm
        if farm not in selected_farms:
            continue
        dated = [event for event in resp_events if event.event_date is not None]
        if inventory is not None and inventory.bdat is not None:
            dated = [event for event in dated if event.event_date >= inventory.bdat]
        starts = episode_start_dates([event.event_date for event in dated if event.event_date])
        samples = samples_by_animal.get(animal_id, [])
        exits = [
            event.event_date
            for event in by_animal_exit.get(animal_id, [])
            if event.event_date is not None
        ]
        etag_value = (inventory.etag if inventory else None) or animal_id
        breed = breed_for(inventory, dated)
        for index, episode_start in enumerate(starts):
            if episode_start < start or episode_start > end:
                continue
            window_end = episode_start + dt.timedelta(days=DEFAULT_DISEASE_EPISODE_GAP_DAYS)
            in_episode = [
                event
                for event in dated
                if event.event_date is not None and episode_start <= event.event_date <= window_end
            ]
            daily = daily_averages(samples, episode_start)
            if not daily:
                skipped_no_health += 1
                continue
            product_id = classify_antibiotic(in_episode)
            start_value = starting_health_value(daily)
            start_band = starting_band_for(start_value)
            age_days = _age_at_treatment(episode_start, inventory, samples)
            episode_age_band = age_band_for(age_days)
            recovered = days_to_recover(daily)
            observed = min(WINDOW_AFTER, (today - episode_start).days)
            exit_on = min(
                (day for day in exits if day >= episode_start),
                default=None,
            )
            left = False
            if exit_on is not None:
                exit_offset = (exit_on - episode_start).days
                if 0 <= exit_offset <= WINDOW_AFTER:
                    observed = min(observed, exit_offset)
                    left = recovered is None or recovered > exit_offset
            next_start = starts[index + 1] if index + 1 < len(starts) else None
            relapsed = bool(
                next_start is not None
                and 0 < (next_start - episode_start).days <= RELAPSE_DAYS
            )
            recovered7 = recovered_by_day(daily, 7)
            recovered14 = recovered_by_day(daily, 14)
            eligible_day7 = observed >= 7
            eligible_day14 = observed >= 14
            eligible_relapse = (today - episode_start).days >= RELAPSE_DAYS
            follow_up_complete = observed >= WINDOW_AFTER
            episodes.append(
                {
                    "animal_id": animal_id,
                    "etag4": etag4(etag_value) or etag4(animal_id),
                    "farm": farm,
                    "breed": breed,
                    "breed_label": BREED_LABELS[breed],
                    "event_date": episode_start,
                    "treatment_number": index + 1,
                    "treatment_label": event_count_label(index + 1),
                    "product": product_id,
                    "product_label": PRODUCT_LABELS[product_id],
                    "age_days": age_days,
                    "age_band": episode_age_band,
                    "starting_health": _round(start_value),
                    "starting_band": start_band,
                    "days_to_recover": recovered,
                    "recovered_by_day7": recovered7,
                    "recovered_by_day14": recovered14,
                    "eligible_day7": eligible_day7,
                    "eligible_day14": eligible_day14,
                    "eligible_relapse": eligible_relapse,
                    "relapsed": relapsed,
                    "left_system": left,
                    "follow_up_complete": follow_up_complete,
                    "observed_days": observed,
                    "remark": _first_remark(in_episode),
                    "daily": daily,
                    "day14_health": _round(daily.get(WINDOW_AFTER) or daily.get(observed)),
                    "latest_health": _round(
                        next(
                            (
                                daily[offset]
                                for offset in range(min(observed, WINDOW_AFTER), -WINDOW_BEFORE - 1, -1)
                                if offset in daily
                            ),
                            None,
                        )
                    ),
                }
            )

    filtered = [episode for episode in episodes if episode["breed"] in selected_breeds]
    if starting_band:
        filtered = [episode for episode in filtered if episode["starting_band"] == starting_band]
    if age_band:
        filtered = [episode for episode in filtered if episode["age_band"] == age_band]

    numbers_present = sorted({episode["treatment_number"] for episode in filtered})
    if selected_event_counts is None:
        selected_event_counts = list(numbers_present)
    event_count_options = [
        {
            "id": number,
            "label": event_count_label(number),
            "episodes": sum(1 for episode in filtered if episode["treatment_number"] == number),
            "selected": number in selected_event_counts,
        }
        for number in numbers_present
    ]
    counts_by_product = {
        key: sum(1 for episode in filtered if episode["product"] == key)
        for key in PRODUCT_ORDER
    }
    if selected_products is None:
        threshold = MIN_DEFAULT_EPISODES if min_episodes is None else min_episodes
        selected_products = [
            key for key in PRODUCT_ORDER if counts_by_product[key] > threshold
        ]
    product_counts = [
        {
            "id": key,
            "label": PRODUCT_LABELS[key],
            "episodes": counts_by_product[key],
            "selected": key in selected_products,
        }
        for key in PRODUCT_ORDER
    ]
    filtered = [
        episode
        for episode in filtered
        if episode["treatment_number"] in selected_event_counts
        and episode["product"] in selected_products
    ]

    summary = [
        _summary_row(key, PRODUCT_LABELS[key], [episode for episode in filtered if episode["product"] == key])
        for key in PRODUCT_ORDER
        if any(episode["product"] == key for episode in filtered)
    ]
    if len(summary) > 1:
        summary.append(_summary_row("total", "All treatments", filtered))
    elif len(summary) == 1:
        summary[0] = _summary_row(summary[0]["product"], summary[0]["label"], filtered)

    unrecovered = [
        {
            "animal_id": episode["animal_id"],
            "etag4": episode["etag4"],
            "farm": episode["farm"],
            "breed": episode["breed"],
            "breed_label": episode["breed_label"],
            "event_date": episode["event_date"].isoformat(),
            "treatment_number": episode["treatment_number"],
            "treatment_label": episode["treatment_label"],
            "product": episode["product"],
            "product_label": episode["product_label"],
            "age_days": episode["age_days"],
            "starting_health": episode["starting_health"],
            "starting_band": episode["starting_band"],
            "day14_health": episode["day14_health"],
            "latest_health": episode["latest_health"],
            "remark": episode["remark"],
            "daily": [
                {"offset": offset, "health_index": _round(episode["daily"].get(offset))}
                for offset in OFFSETS
            ],
        }
        for episode in filtered
        if episode["follow_up_complete"]
        and not episode["recovered_by_day14"]
        and not episode["left_system"]
    ]
    unrecovered.sort(key=lambda item: (item["event_date"], item["animal_id"]), reverse=True)

    payload = _empty_payload(
        date_from=start,
        date_to=end,
        farms=selected_farms,
        starting_band=starting_band,
        age_band=age_band,
        products=selected_products,
        breeds=selected_breeds,
        event_counts=selected_event_counts,
        event_count_options=event_count_options,
        fiscal_year=resolved_year,
        fiscal_year_options=fiscal_year_options,
        date_bounds=date_bounds,
        product_counts=product_counts,
    )
    payload["episode_count"] = len(filtered)
    payload["skipped_no_health"] = skipped_no_health
    payload["curve"]["series"] = _curve_series(filtered)
    payload["summary"] = summary
    payload["unrecovered"] = unrecovered
    return payload
