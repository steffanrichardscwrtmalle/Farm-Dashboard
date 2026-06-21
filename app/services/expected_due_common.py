"""Shared expected-due month pivot for stock inventory reports."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, HerdInventory


def normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


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


def build_expected_due_report(
    db: Session,
    *,
    category: str,
    farms: list[str] | None = None,
    breeds: list[str] | None = None,
    include_breed_options: bool = False,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))

    empty_result: dict[str, Any] = {
        "rows": [],
        "grand_total": {"CM": 0, "GAD": 0, "total": 0},
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        empty_result["breed_options"] = _get_breed_options(db, category, selected_farms)

    if not selected_farms:
        return empty_result

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
    month_order: list[tuple[int, str]] = []
    seen_months: set[tuple[int, str]] = set()

    for sort_key, expected_month, farm, count in counts:
        if sort_key is None or not expected_month:
            continue
        key = (int(sort_key), str(expected_month))
        if key not in seen_months:
            seen_months.add(key)
            month_order.append(key)
        pivot.setdefault(key, {"CM": 0, "GAD": 0})
        if farm in pivot[key]:
            pivot[key][farm] = int(count)

    rows: list[dict[str, Any]] = []
    grand_cm = 0
    grand_gad = 0
    for sort_key, expected_month in month_order:
        cm = pivot[(sort_key, expected_month)].get("CM", 0)
        gad = pivot[(sort_key, expected_month)].get("GAD", 0)
        total = cm + gad
        rows.append(
            {
                "expected_month": expected_month,
                "sort_key": sort_key,
                "CM": cm,
                "GAD": gad,
                "total": total,
            }
        )
        grand_cm += cm
        grand_gad += gad

    result: dict[str, Any] = {
        "rows": rows,
        "grand_total": {
            "CM": grand_cm,
            "GAD": grand_gad,
            "total": grand_cm + grand_gad,
        },
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        result["breed_options"] = _get_breed_options(db, category, selected_farms)
    return result
