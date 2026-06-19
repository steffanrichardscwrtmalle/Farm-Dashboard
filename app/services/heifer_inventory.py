"""Heifer inventory report from herd_inventory table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, HerdInventory


def _normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def get_heifer_inventory_report(
    db: Session,
    farms: list[str] | None = None,
    min_age: int | None = None,
    max_age: int | None = None,
) -> dict[str, Any]:
    selected_farms = _normalize_farms(farms)

    bounds_row = db.execute(
        select(
            func.min(HerdInventory.months_old),
            func.max(HerdInventory.months_old),
        )
        .where(HerdInventory.category == "Youngstock")
        .where(HerdInventory.gender == "Female")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old.isnot(None))
    ).one()

    data_min = int(bounds_row[0]) if bounds_row[0] is not None else 0
    data_max = int(bounds_row[1]) if bounds_row[1] is not None else 0

    effective_min = data_min if min_age is None else max(min_age, data_min)
    effective_max = data_max if max_age is None else min(max_age, data_max)

    if effective_min > effective_max:
        latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
        return {
            "rows": [],
            "grand_total": {"CM": 0, "GAD": 0, "total": 0},
            "age_bounds": {"min": data_min, "max": data_max},
            "latest_import": latest_import.isoformat() if latest_import else None,
        }

    counts = db.execute(
        select(
            HerdInventory.months_old,
            HerdInventory.farm,
            func.count(),
        )
        .where(HerdInventory.category == "Youngstock")
        .where(HerdInventory.gender == "Female")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old >= effective_min)
        .where(HerdInventory.months_old <= effective_max)
        .group_by(HerdInventory.months_old, HerdInventory.farm)
        .order_by(HerdInventory.months_old)
    ).all()

    pivot: dict[int, dict[str, int]] = {}
    for months_old, farm, count in counts:
        age = int(months_old)
        pivot.setdefault(age, {"CM": 0, "GAD": 0})
        if farm in pivot[age]:
            pivot[age][farm] = int(count)

    rows: list[dict[str, Any]] = []
    grand_cm = 0
    grand_gad = 0
    for age in range(effective_min, effective_max + 1):
        cm = pivot.get(age, {}).get("CM", 0)
        gad = pivot.get(age, {}).get("GAD", 0)
        total = cm + gad
        rows.append({"months_old": age, "CM": cm, "GAD": gad, "total": total})
        grand_cm += cm
        grand_gad += gad

    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))

    return {
        "rows": rows,
        "grand_total": {
            "CM": grand_cm,
            "GAD": grand_gad,
            "total": grand_cm + grand_gad,
        },
        "age_bounds": {"min": data_min, "max": data_max},
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
