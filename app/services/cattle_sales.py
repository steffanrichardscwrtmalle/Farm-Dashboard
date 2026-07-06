"""Cattle sales listing with herd event linkage."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CattleSaleLine, CowEvent, HERD_FARM_OPTIONS
from app.services.cattle_sale_pdf import is_rejected_sale, normalize_etag
from app.services.events_common import normalize_farms
from app.services.stock_group import (
    stock_group_from_event_fields,
    valuation_category_from_stock_group,
)

SOLD_EVENT = "SOLD"
EVENT_MATCH_WINDOW_DAYS = 14
CATTLE_CATEGORIES: tuple[str, ...] = ("Dairy", "Youngstock", "Beef")


def format_age_years_months(age_days: int | None) -> str | None:
    if age_days is None or age_days < 0:
        return None
    years = age_days // 365
    months = (age_days % 365) // 30
    if years and months:
        return f"{years}y {months}m"
    if years:
        return f"{years}y"
    if months:
        return f"{months}m"
    return "0m"


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def compute_dim_at_cull(
    *,
    lact: int | None,
    event_date: dt.date,
    bdat: dt.date | None,
    fdat: dt.date | None,
    dim_field: float | None,
) -> int | None:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        if dim_field is not None:
            try:
                return int(round(float(dim_field)))
            except (TypeError, ValueError):
                pass
        if fdat is not None:
            days = (event_date - fdat).days
            return days if days >= 0 else None
        return None
    if bdat is not None:
        days = (event_date - bdat).days
        return days if days >= 0 else None
    return None


def compute_price_per_kg(amount_gbp: float, cold_weight_kg: float) -> float | None:
    if cold_weight_kg <= 0:
        return None
    return round(amount_gbp / cold_weight_kg, 2)


def _category_from_event(lact: int | None, cbrd: int | None, gndr: str | None) -> str:
    stock_group = stock_group_from_event_fields(lact, cbrd, gndr)
    return valuation_category_from_stock_group(stock_group)


def _load_sold_events(
    db: Session,
    farms: list[str],
    etags: set[str],
    min_date: dt.date,
    max_date: dt.date,
) -> dict[tuple[str, str], list[CowEvent]]:
    if not etags:
        return {}
    normalized_etags = {normalize_etag(etag) for etag in etags}
    normalized_etags.discard("")
    if not normalized_etags:
        return {}

    window_start = min_date - dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    window_end = max_date + dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    rows = db.scalars(
        select(CowEvent).where(
            CowEvent.event == SOLD_EVENT,
            CowEvent.farm.in_(farms),
            CowEvent.event_date.isnot(None),
            CowEvent.event_date >= window_start,
            CowEvent.event_date <= window_end,
        )
    ).all()
    grouped: dict[tuple[str, str], list[CowEvent]] = {}
    for row in rows:
        etag = normalize_etag(row.etag)
        if not etag or etag not in normalized_etags:
            continue
        key = (row.farm, etag)
        grouped.setdefault(key, []).append(row)
    for events in grouped.values():
        events.sort(key=lambda e: e.event_date or dt.date.min)
    return grouped


def _best_sold_match(
    events: list[CowEvent],
    sale_date: dt.date,
    kill_date: dt.date | None = None,
) -> CowEvent | None:
    if not events:
        return None
    reference_dates = [sale_date]
    if kill_date is not None and kill_date != sale_date:
        reference_dates.append(kill_date)
    best: CowEvent | None = None
    best_delta: int | None = None
    for event in events:
        if event.event_date is None:
            continue
        for reference_date in reference_dates:
            delta = abs((event.event_date - reference_date).days)
            if delta > EVENT_MATCH_WINDOW_DAYS:
                continue
            if best is None or delta < best_delta:
                best = event
                best_delta = delta
    return best


def normalize_categories(categories: list[str] | None) -> list[str] | None:
    if not categories:
        return None
    selected = [c for c in categories if c in CATTLE_CATEGORIES]
    return selected or None


def list_cattle_sales(
    db: Session,
    *,
    farms: list[str] | None = None,
    categories: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    include_unmatched: bool = True,
    include_date_bounds: bool = True,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_categories = normalize_categories(categories)
    if not selected_farms:
        return {
            "rows": [],
            "total": 0,
            "date_bounds": None,
            "charts": {"cold_weight_vs_date": [], "amount_vs_date": [], "amount_vs_dim": []},
        }

    query = select(CattleSaleLine).where(CattleSaleLine.farm.in_(selected_farms))
    if date_from is not None:
        query = query.where(CattleSaleLine.sale_date >= date_from)
    if date_to is not None:
        query = query.where(CattleSaleLine.sale_date <= date_to)
    query = query.order_by(
        CattleSaleLine.sale_date.desc(),
        CattleSaleLine.farm.asc(),
        CattleSaleLine.etag.asc(),
    )
    sale_lines = list(db.scalars(query).all())

    date_bounds = None
    if include_date_bounds:
        bounds = db.execute(
            select(
                func.min(CattleSaleLine.sale_date),
                func.max(CattleSaleLine.sale_date),
            ).where(CattleSaleLine.farm.in_(selected_farms))
        ).one()
        min_date, max_date = bounds[0], bounds[1]
        if min_date and max_date:
            date_bounds = {"min": min_date.isoformat(), "max": max_date.isoformat()}

    if not sale_lines:
        return {
            "rows": [],
            "total": 0,
            "date_bounds": date_bounds,
            "charts": {"cold_weight_vs_date": [], "amount_vs_date": [], "amount_vs_dim": []},
        }

    etags = {line.etag for line in sale_lines}
    reference_dates: list[dt.date] = []
    for line in sale_lines:
        reference_dates.append(line.sale_date)
        if line.kill_date is not None:
            reference_dates.append(line.kill_date)
    min_sale = min(reference_dates)
    max_sale = max(reference_dates)
    sold_by_key = _load_sold_events(db, selected_farms, etags, min_sale, max_sale)

    rows: list[dict[str, Any]] = []
    for line in sale_lines:
        norm_etag = normalize_etag(line.etag)
        match = _best_sold_match(
            sold_by_key.get((line.farm, norm_etag), []),
            line.sale_date,
            line.kill_date,
        )
        event_matched = match is not None
        if not event_matched and not include_unmatched:
            continue

        cow_id = None
        age_display = None
        dim_value = None
        lact = None
        category = None
        event_date = None

        if match is not None:
            cow_id = (match.cow_id or "").strip() or None
            lact = _normalize_lact(match.lact)
            category = _category_from_event(match.lact, match.cbrd, match.gndr)
            event_date = match.event_date
            if match.bdat and match.event_date:
                age_days = (match.event_date - match.bdat).days
                age_display = format_age_years_months(age_days)
            dim_value = compute_dim_at_cull(
                lact=lact,
                event_date=match.event_date,
                bdat=match.bdat,
                fdat=match.fdat,
                dim_field=match.dim,
            )

        if selected_categories and category is not None and category not in selected_categories:
            continue

        rejected = is_rejected_sale(
            line.cold_weight_kg, line.reject_kg, line.amount_gbp
        )
        price_per_kg = (
            None
            if rejected
            else compute_price_per_kg(line.amount_gbp, line.cold_weight_kg)
        )
        rows.append(
            {
                "farm": line.farm,
                "cow_id": cow_id,
                "etag": line.etag,
                "age": age_display,
                "dim": dim_value,
                "lact": lact,
                "category": category,
                "cold_weight_kg": line.cold_weight_kg,
                "reject_kg": line.reject_kg,
                "kill_date": line.kill_date.isoformat() if line.kill_date else None,
                "amount_gbp": line.amount_gbp,
                "is_rejected": rejected,
                "price_per_kg": price_per_kg,
                "sale_date": line.sale_date.isoformat(),
                "event_date": event_date.isoformat() if event_date else None,
                "event_matched": event_matched,
            }
        )

    charts = {
        "cold_weight_vs_date": [
            {"x": r["sale_date"], "y": r["cold_weight_kg"], "farm": r["farm"], "etag": r["etag"]}
            for r in rows
            if r["cold_weight_kg"] is not None
        ],
        "amount_vs_date": [
            {"x": r["sale_date"], "y": r["amount_gbp"], "farm": r["farm"], "etag": r["etag"]}
            for r in rows
            if r["amount_gbp"] is not None and not r.get("is_rejected")
        ],
        "amount_vs_dim": [
            {"x": r["dim"], "y": r["amount_gbp"], "farm": r["farm"], "etag": r["etag"]}
            for r in rows
            if r["dim"] is not None
            and r["amount_gbp"] is not None
            and not r.get("is_rejected")
        ],
    }

    return {
        "rows": rows,
        "total": len(rows),
        "date_bounds": date_bounds,
        "charts": charts,
        "categories": list(CATTLE_CATEGORIES),
        "farms": list(HERD_FARM_OPTIONS),
    }
