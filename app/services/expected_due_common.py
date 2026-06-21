"""Shared expected-due month pivot for stock inventory reports."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, HerdInventory


def normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def _fiscal_year_from_date(value: dt.date) -> int:
    return value.year + 1 if value.month >= 4 else value.year


def _sort_key_from_date(value: dt.date) -> int:
    month = value.month
    fiscal_year = _fiscal_year_from_date(value)
    month_adjusted = month - 3 if month >= 4 else month + 9
    return fiscal_year * 100 + month_adjusted


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _iter_month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    current = _month_start(start)
    end_month = _month_start(end)
    months: list[dt.date] = []
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def _month_count_inclusive(due_from: dt.date, due_to: dt.date) -> int:
    return len(_iter_month_starts(due_from, due_to))


def _get_date_bounds(
    db: Session,
    category: str,
    selected_farms: list[str],
) -> tuple[dt.date | None, dt.date | None]:
    row = db.execute(
        select(
            func.min(HerdInventory.expected_due),
            func.max(HerdInventory.expected_due),
        )
        .where(HerdInventory.category == category)
        .where(HerdInventory.gender == "Female")
        .where(HerdInventory.expected_due.isnot(None))
        .where(HerdInventory.expected_month.isnot(None))
        .where(HerdInventory.farm.in_(selected_farms))
    ).one()
    min_date = row[0]
    max_date = row[1]
    if min_date is None or max_date is None:
        return None, None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return min_date, max_date


def _get_breed_options(db: Session, category: str, selected_farms: list[str]) -> list[str]:
    rows = db.execute(
        select(HerdInventory.lsbrd)
        .where(HerdInventory.category == category)
        .where(HerdInventory.gender == "Female")
        .where(HerdInventory.expected_due.isnot(None))
        .where(HerdInventory.expected_month.isnot(None))
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.lsbrd.isnot(None))
        .where(HerdInventory.lsbrd != "")
        .distinct()
        .order_by(HerdInventory.lsbrd)
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def _zero_fill_rows(
    pivot: dict[tuple[int, str], dict[str, int]],
    due_from: dt.date,
    due_to: dt.date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(due_from, due_to):
        sort_key = _sort_key_from_date(month_start)
        expected_month = month_start.strftime("%b-%y")
        key = (sort_key, expected_month)
        counts = pivot.get(key, {"CM": 0, "GAD": 0})
        cm = counts.get("CM", 0)
        gad = counts.get("GAD", 0)
        rows.append(
            {
                "expected_month": expected_month,
                "sort_key": sort_key,
                "CM": cm,
                "GAD": gad,
                "total": cm + gad,
            }
        )
    return rows


def build_expected_due_report(
    db: Session,
    *,
    category: str,
    farms: list[str] | None = None,
    breeds: list[str] | None = None,
    due_from: dt.date | None = None,
    due_to: dt.date | None = None,
    include_breed_options: bool = False,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))

    empty_result: dict[str, Any] = {
        "rows": [],
        "grand_total": {"CM": 0, "GAD": 0, "total": 0},
        "range_summary": {"total": 0, "month_count": 0, "average_per_month": 0},
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        empty_result["breed_options"] = _get_breed_options(db, category, selected_farms)

    if not selected_farms:
        return empty_result

    bounds_min, bounds_max = _get_date_bounds(db, category, selected_farms)
    if bounds_min is None or bounds_max is None:
        empty_result["date_bounds"] = None
        if include_breed_options:
            empty_result["breed_options"] = _get_breed_options(db, category, selected_farms)
        return empty_result

    date_bounds = {
        "min": bounds_min.isoformat(),
        "max": bounds_max.isoformat(),
    }

    effective_from = due_from if due_from is not None else bounds_min
    effective_to = due_to if due_to is not None else bounds_max
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    query = (
        select(
            HerdInventory.sort_key,
            HerdInventory.expected_month,
            HerdInventory.farm,
            func.count(),
        )
        .where(HerdInventory.category == category)
        .where(HerdInventory.gender == "Female")
        .where(HerdInventory.expected_due.isnot(None))
        .where(HerdInventory.expected_month.isnot(None))
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.expected_due >= effective_from)
        .where(HerdInventory.expected_due <= effective_to)
    )

    if breeds:
        query = query.where(HerdInventory.lsbrd.in_(breeds))

    counts = db.execute(
        query.group_by(
            HerdInventory.sort_key,
            HerdInventory.expected_month,
            HerdInventory.farm,
        ).order_by(HerdInventory.sort_key)
    ).all()

    pivot: dict[tuple[int, str], dict[str, int]] = {}
    for sort_key, expected_month, farm, count in counts:
        if sort_key is None or not expected_month:
            continue
        key = (int(sort_key), str(expected_month))
        pivot.setdefault(key, {"CM": 0, "GAD": 0})
        if farm in pivot[key]:
            pivot[key][farm] = int(count)

    rows = _zero_fill_rows(pivot, effective_from, effective_to)

    grand_cm = sum(row["CM"] for row in rows)
    grand_gad = sum(row["GAD"] for row in rows)
    grand_total = grand_cm + grand_gad
    month_count = _month_count_inclusive(effective_from, effective_to)

    result: dict[str, Any] = {
        "rows": rows,
        "grand_total": {
            "CM": grand_cm,
            "GAD": grand_gad,
            "total": grand_total,
        },
        "date_bounds": date_bounds,
        "range_summary": {
            "total": grand_total,
            "month_count": month_count,
            "average_per_month": round(grand_total / month_count, 1) if month_count else 0,
        },
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        result["breed_options"] = _get_breed_options(db, category, selected_farms)
    return result
