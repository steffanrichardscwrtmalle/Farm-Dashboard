"""Derived stock purchase listing for Office Admin."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import PURCHASE_STOCK_GROUP_OPTIONS, STOCK_GROUP_OPTIONS, StockPurchaseAnimal
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    normalize_farms,
)

VALID_STOCK_GROUPS = set(STOCK_GROUP_OPTIONS)
VALID_PURCHASE_GROUPS = set(PURCHASE_STOCK_GROUP_OPTIONS)


def normalize_stock_group(value: str | None) -> str:
    normalized = (value or STOCK_GROUP_OPTIONS[0]).strip().lower()
    if normalized not in VALID_STOCK_GROUPS:
        return STOCK_GROUP_OPTIONS[0]
    return normalized


def normalize_purchase_stock_group(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_PURCHASE_GROUPS:
        return None
    return normalized


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _last_day_of_month(month_start: dt.date) -> dt.date:
    if month_start.month == 12:
        next_month = dt.date(month_start.year + 1, 1, 1)
    else:
        next_month = dt.date(month_start.year, month_start.month + 1, 1)
    return next_month - dt.timedelta(days=1)


def _selected_purchase_groups(
    stock_groups: list[str] | None,
) -> list[str]:
    return [
        group
        for group in (
            normalize_purchase_stock_group(value) for value in (stock_groups or [])
        )
        if group
    ]


def _purchase_base_filters(
    query,
    *,
    selected_farms: list[str],
    selected_groups: list[str],
):
    if selected_farms:
        query = query.where(StockPurchaseAnimal.farm.in_(selected_farms))
    if selected_groups:
        query = query.where(StockPurchaseAnimal.stock_group.in_(selected_groups))
    return query


def _edat_bounds_for_selection(
    db: Session,
    *,
    selected_farms: list[str],
    selected_groups: list[str],
) -> tuple[dt.date, dt.date] | None:
    query = select(
        func.min(StockPurchaseAnimal.edat),
        func.max(StockPurchaseAnimal.edat),
    ).where(StockPurchaseAnimal.edat.isnot(None))
    query = _purchase_base_filters(
        query, selected_farms=selected_farms, selected_groups=selected_groups
    )
    min_edat, max_edat = db.execute(query).one()
    if min_edat is None or max_edat is None:
        return None
    return _month_start(min_edat), _month_start(max_edat)


def _fiscal_year_options_for_selection(
    db: Session,
    *,
    selected_farms: list[str],
    selected_groups: list[str],
) -> list[int]:
    query = select(StockPurchaseAnimal.edat).where(StockPurchaseAnimal.edat.isnot(None))
    query = _purchase_base_filters(
        query, selected_farms=selected_farms, selected_groups=selected_groups
    )
    years = {
        _fiscal_year_from_date(day)
        for day in db.scalars(query).all()
        if isinstance(day, dt.date)
    }
    return sorted(years, reverse=True)


def list_stock_purchases(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_groups: list[str] | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_groups = _selected_purchase_groups(stock_groups)

    bounds = _edat_bounds_for_selection(
        db, selected_farms=selected_farms, selected_groups=selected_groups
    )
    fiscal_year_options = _fiscal_year_options_for_selection(
        db, selected_farms=selected_farms, selected_groups=selected_groups
    )
    latest_import = db.scalar(select(func.max(StockPurchaseAnimal.import_timestamp)))

    if bounds is None:
        return {
            "rows": [],
            "total": 0,
            "summary_rows": [],
            "latest_import": latest_import.isoformat() if latest_import else None,
            "date_bounds": None,
            "fiscal_year_options": fiscal_year_options,
            "selected_fiscal_year": fiscal_year,
        }

    bounds_min, bounds_max = bounds
    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
        slider_min = _month_start(slider_min)
        slider_max = _month_start(slider_max)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    effective_from = month_from if month_from is not None else slider_min
    effective_to = month_to if month_to is not None else slider_max
    effective_from = max(_month_start(effective_from), slider_min, bounds_min)
    effective_to_month = min(_month_start(effective_to), slider_max, bounds_max)
    if effective_from > effective_to_month:
        effective_from, effective_to_month = effective_to_month, effective_from
    effective_to = _last_day_of_month(effective_to_month)

    query = select(StockPurchaseAnimal).order_by(
        StockPurchaseAnimal.edat.desc(),
        StockPurchaseAnimal.farm.asc(),
        StockPurchaseAnimal.etag.asc(),
    )
    query = _purchase_base_filters(
        query, selected_farms=selected_farms, selected_groups=selected_groups
    )
    query = query.where(
        StockPurchaseAnimal.edat >= effective_from,
        StockPurchaseAnimal.edat <= effective_to,
    )

    records = list(db.scalars(query).all())

    summary_query = (
        select(
            StockPurchaseAnimal.farm,
            StockPurchaseAnimal.stock_group,
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
            func.count(),
        )
        .group_by(
            StockPurchaseAnimal.farm,
            StockPurchaseAnimal.stock_group,
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
        )
        .order_by(
            extract("year", StockPurchaseAnimal.edat).desc(),
            extract("month", StockPurchaseAnimal.edat).desc(),
            StockPurchaseAnimal.farm.asc(),
        )
    )
    summary_query = _purchase_base_filters(
        summary_query, selected_farms=selected_farms, selected_groups=selected_groups
    )
    summary_query = summary_query.where(
        StockPurchaseAnimal.edat >= effective_from,
        StockPurchaseAnimal.edat <= effective_to,
    )

    summary_rows: list[dict[str, Any]] = []
    for farm, group, year, month, count in db.execute(summary_query).all():
        if year is None or month is None:
            continue
        month_start = dt.date(int(year), int(month), 1)
        summary_rows.append(
            {
                "farm": str(farm),
                "stock_group": str(group),
                "month_start": month_start.isoformat(),
                "quantity": int(count),
            }
        )

    return {
        "rows": [record.to_dict() for record in records],
        "total": len(records),
        "summary_rows": summary_rows,
        "latest_import": latest_import.isoformat() if latest_import else None,
        "date_bounds": {
            "min": max(slider_min, bounds_min).isoformat(),
            "max": _last_day_of_month(min(slider_max, bounds_max)).isoformat(),
        },
        "fiscal_year_options": fiscal_year_options,
        "selected_fiscal_year": fiscal_year,
    }
