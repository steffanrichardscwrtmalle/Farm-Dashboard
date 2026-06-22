"""Monthly stock movement report for Office Admin stock accruals."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, extract, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    CowEvent,
    HerdBirth,
    StockOpeningBaseline,
    StockPurchaseRecord,
)
from app.services.events_common import (
    SALES_TABLE_REASON_ORDER,
    _fiscal_year_calendar_bounds,
    _iter_month_starts,
    _sales_reason_expression,
    normalize_farms,
)
from app.services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_DAIRY
from app.services.stock_purchases import normalize_stock_group

_ZERO_SALES = {reason: 0 for reason in SALES_TABLE_REASON_ORDER}


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _month_key(value: dt.date) -> tuple[int, int]:
    return (value.year, value.month)


def _apply_cow_event_stock_group(query, stock_group: str):
    if stock_group == STOCK_GROUP_COWS:
        return query.where(CowEvent.lact.isnot(None)).where(CowEvent.lact > 0)
    return (
        query.where(CowEvent.lact == 0)
        .where(func.upper(func.coalesce(CowEvent.gndr, "")) == "F")
    )


def _fetch_sales_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], dict[str, int]]:
    reason_expr = _sales_reason_expression()
    query = (
        select(
            extract("year", CowEvent.event_date),
            extract("month", CowEvent.event_date),
            reason_expr.label("reason"),
            func.count(),
        )
        .where(CowEvent.event == "SOLD")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm == farm)
        .where(CowEvent.event_date >= month_from)
        .where(CowEvent.event_date <= month_to)
    )
    query = _apply_cow_event_stock_group(query, stock_group)
    query = query.group_by(
        extract("year", CowEvent.event_date),
        extract("month", CowEvent.event_date),
        reason_expr,
    )

    pivot: dict[tuple[int, int], dict[str, int]] = {}
    for year, month, reason, count in db.execute(query).all():
        if year is None or month is None or reason is None:
            continue
        key = (int(year), int(month))
        pivot.setdefault(key, dict(_ZERO_SALES))
        reason_key = str(reason)
        if reason_key in pivot[key]:
            pivot[key][reason_key] = int(count)
    return pivot


def _fetch_event_count_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    event_type: str,
    month_from: dt.date,
    month_to: dt.date,
    lact_filter: str | None = None,
) -> dict[tuple[int, int], int]:
    query = (
        select(
            extract("year", CowEvent.event_date),
            extract("month", CowEvent.event_date),
            func.count(),
        )
        .where(CowEvent.event == event_type)
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm == farm)
        .where(CowEvent.event_date >= month_from)
        .where(CowEvent.event_date <= month_to)
    )
    if lact_filter == "fresh_heifers":
        query = query.where(CowEvent.lact == 1)
    else:
        query = _apply_cow_event_stock_group(query, stock_group)

    query = query.group_by(
        extract("year", CowEvent.event_date),
        extract("month", CowEvent.event_date),
    )

    return {
        (int(year), int(month)): int(count)
        for year, month, count in db.execute(query).all()
        if year is not None and month is not None
    }


def _fetch_births_by_month(
    db: Session,
    *,
    farm: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], int]:
    query = (
        select(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
            func.count(),
        )
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm == farm)
        .where(
            or_(
                HerdBirth.category == CATEGORY_DAIRY,
                and_(HerdBirth.category.is_(None), HerdBirth.cbrd < BEEF_CBREED_MIN),
            )
        )
        .where(func.upper(func.coalesce(HerdBirth.gndr, "")) == "F")
        .where(HerdBirth.bdat >= month_from)
        .where(HerdBirth.bdat <= month_to)
        .group_by(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
        )
    )
    return {
        (int(year), int(month)): int(count)
        for year, month, count in db.execute(query).all()
        if year is not None and month is not None
    }


def _fetch_purchases_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], int]:
    rows = db.execute(
        select(StockPurchaseRecord.month_start, StockPurchaseRecord.quantity)
        .where(StockPurchaseRecord.farm == farm)
        .where(StockPurchaseRecord.stock_group == stock_group)
        .where(StockPurchaseRecord.month_start >= _month_start(month_from))
        .where(StockPurchaseRecord.month_start <= _month_start(month_to))
    ).all()
    return {_month_key(month_start): int(quantity) for month_start, quantity in rows}


def _last_day_of_month(value: dt.date) -> dt.date:
    if value.month == 12:
        return dt.date(value.year, 12, 31)
    return dt.date(value.year, value.month + 1, 1) - dt.timedelta(days=1)


def _get_baseline(
    db: Session,
    farm: str,
    stock_group: str,
) -> StockOpeningBaseline | None:
    return db.scalar(
        select(StockOpeningBaseline).where(
            StockOpeningBaseline.farm == farm,
            StockOpeningBaseline.stock_group == stock_group,
        )
    )


def _compute_farm_rows(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    display_from: dt.date,
    display_to: dt.date,
) -> list[dict[str, Any]]:
    baseline = _get_baseline(db, farm, stock_group)
    if baseline is None:
        return []

    baseline_month = _month_start(baseline.month_start)
    end_month = _month_start(display_to)
    if end_month < baseline_month:
        return []

    calc_end = _last_day_of_month(end_month)
    sales = _fetch_sales_by_month(
        db, farm=farm, stock_group=stock_group, month_from=baseline_month, month_to=calc_end
    )
    deaths = _fetch_event_count_by_month(
        db,
        farm=farm,
        stock_group=stock_group,
        event_type="DIED",
        month_from=baseline_month,
        month_to=calc_end,
    )
    purchases = _fetch_purchases_by_month(
        db,
        farm=farm,
        stock_group=stock_group,
        month_from=baseline_month,
        month_to=calc_end,
    )

    births: dict[tuple[int, int], int] = {}
    calvings = _fetch_event_count_by_month(
        db,
        farm=farm,
        stock_group=stock_group,
        event_type="FRESH",
        month_from=baseline_month,
        month_to=calc_end,
        lact_filter="fresh_heifers",
    )
    if stock_group == STOCK_GROUP_YOUNGSTOCK:
        births = _fetch_births_by_month(
            db, farm=farm, month_from=baseline_month, month_to=calc_end
        )

    all_months = _iter_month_starts(baseline_month, end_month)
    opening = baseline.opening_count
    computed: list[dict[str, Any]] = []

    for month_start in all_months:
        key = _month_key(month_start)
        month_sales = sales.get(key, dict(_ZERO_SALES))
        sales_total = sum(month_sales.values())
        month_deaths = deaths.get(key, 0)
        month_births = births.get(key, 0) if stock_group == STOCK_GROUP_YOUNGSTOCK else 0
        raw_calvings = calvings.get(key, 0)
        month_calvings = raw_calvings if stock_group == STOCK_GROUP_COWS else -raw_calvings
        month_purchases = purchases.get(key, 0)

        closing = (
            opening
            - sales_total
            - month_deaths
            + month_births
            + month_calvings
            + month_purchases
        )

        row = {
            "month_start": month_start.isoformat(),
            "event_month": month_start.strftime("%b-%y"),
            "opening": opening,
            "sales": dict(month_sales),
            "sales_total": sales_total,
            "deaths": month_deaths,
            "births": month_births,
            "calvings": month_calvings,
            "purchases": month_purchases,
            "closing": closing,
            "warning": closing < 0 or opening < 0,
        }
        computed.append(row)
        opening = closing

    display_from_month = _month_start(display_from)
    display_to_month = _month_start(display_to)
    return [
        row
        for row in computed
        if display_from_month <= dt.date.fromisoformat(row["month_start"]) <= display_to_month
    ]


def _merge_farm_rows(farm_rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not farm_rows:
        return []
    if len(farm_rows) == 1:
        return farm_rows[0]

    by_month: dict[str, dict[str, Any]] = {}
    for rows in farm_rows:
        for row in rows:
            month = row["month_start"]
            if month not in by_month:
                by_month[month] = {
                    "month_start": month,
                    "event_month": row["event_month"],
                    "opening": 0,
                    "sales": dict(_ZERO_SALES),
                    "sales_total": 0,
                    "deaths": 0,
                    "births": 0,
                    "calvings": 0,
                    "purchases": 0,
                    "closing": 0,
                    "warning": False,
                }
            merged = by_month[month]
            merged["opening"] += row["opening"]
            merged["sales_total"] += row["sales_total"]
            merged["deaths"] += row["deaths"]
            merged["births"] += row["births"]
            merged["calvings"] += row["calvings"]
            merged["purchases"] += row["purchases"]
            merged["closing"] += row["closing"]
            merged["warning"] = merged["warning"] or row["warning"]
            for reason in SALES_TABLE_REASON_ORDER:
                merged["sales"][reason] += row["sales"].get(reason, 0)

    return sorted(by_month.values(), key=lambda row: row["month_start"])


def build_stock_accruals_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_group: str | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    group = normalize_stock_group(stock_group)

    if not selected_farms:
        return {
            "rows": [],
            "sales_reasons": list(SALES_TABLE_REASON_ORDER),
            "stock_group": group,
            "date_bounds": None,
            "fiscal_year_options": [],
            "selected_fiscal_year": fiscal_year,
        }

    baselines = list(
        db.scalars(
            select(StockOpeningBaseline).where(
                StockOpeningBaseline.farm.in_(selected_farms),
                StockOpeningBaseline.stock_group == group,
            )
        ).all()
    )
    if not baselines:
        return {
            "rows": [],
            "sales_reasons": list(SALES_TABLE_REASON_ORDER),
            "stock_group": group,
            "date_bounds": None,
            "fiscal_year_options": [],
            "selected_fiscal_year": fiscal_year,
        }

    fiscal_year_options = sorted(
        {
            int(value)
            for value in db.scalars(
                select(CowEvent.fiscal_year)
                .where(CowEvent.fiscal_year.isnot(None))
                .where(CowEvent.farm.in_(selected_farms))
                .distinct()
            ).all()
            if value is not None
        },
        reverse=True,
    )

    bounds_min = min(_month_start(b.month_start) for b in baselines)
    bounds_max = dt.date.today().replace(day=1)

    latest_event = db.scalar(
        select(func.max(CowEvent.event_date)).where(CowEvent.farm.in_(selected_farms))
    )
    latest_birth = db.scalar(
        select(func.max(HerdBirth.bdat)).where(HerdBirth.farm.in_(selected_farms))
    )
    for candidate in (latest_event, latest_birth):
        if candidate is not None:
            candidate_month = _month_start(candidate)
            if candidate_month > bounds_max:
                bounds_max = candidate_month

    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
        slider_min = _month_start(slider_min)
        slider_max = _month_start(slider_max)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    effective_from = month_from if month_from is not None else slider_min
    effective_to = month_to if month_to is not None else slider_max
    effective_from = max(_month_start(effective_from), slider_min, bounds_min)
    effective_to = min(_month_start(effective_to), slider_max, bounds_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    per_farm = [
        _compute_farm_rows(
            db,
            farm=farm,
            stock_group=group,
            display_from=effective_from,
            display_to=effective_to,
        )
        for farm in selected_farms
    ]
    rows = _merge_farm_rows(per_farm)

    return {
        "rows": rows,
        "sales_reasons": list(SALES_TABLE_REASON_ORDER),
        "stock_group": group,
        "date_bounds": {
            "min": max(slider_min, bounds_min).isoformat(),
            "max": _last_day_of_month(min(slider_max, bounds_max)).isoformat(),
        },
        "fiscal_year_options": fiscal_year_options,
        "selected_fiscal_year": fiscal_year,
    }
