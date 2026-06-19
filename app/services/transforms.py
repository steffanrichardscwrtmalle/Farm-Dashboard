"""Row-based transforms ported from desktop main.py."""

from __future__ import annotations

import datetime
from typing import Any


def cell_to_date(value: Any) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value if not isinstance(value, datetime.datetime) else value.date()
    if isinstance(value, datetime.datetime):
        return value.date()
    return None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_credit_goods_value(goods_value: float | None) -> bool:
    """A credit line has a negative goods value (not price)."""
    return goods_value is not None and goods_value < 0


def clean_and_transform_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    - Remove rows where TOTAL = 0.
    - Apply unit conversions and renames (same rules as desktop clean_and_transform_data).
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        total_val = parse_number(row.get("total"))
        if total_val is not None and total_val == 0:
            continue
        kept.append(dict(row))

    conversion_rules = [
        ("25kg", 25),
        ("20kg", 20),
        ("500kg", 500),
        ("900kg", 900),
        ("kg", 1),
    ]
    rename_rules = [
        ("mt", "Tonne"),
        ("tonnes", "Tonne"),
        ("each", "Each"),
        ("acre", "Acre"),
    ]
    liquid_conversion_rules = [
        ("5lt", 5, 5, "Litre"),
        ("2acre", 2, 2, "Acre"),
    ]

    for row in kept:
        unit_raw = str(row.get("unit") or "").strip()
        row["unit"] = unit_raw
        unit_str = unit_raw.lower().replace(" ", "")
        if not unit_str:
            continue

        renamed = False
        for match_str, new_unit in rename_rules:
            if unit_str == match_str.replace(" ", ""):
                row["unit"] = new_unit
                renamed = True
                break

        if renamed:
            continue

        liquid_matched = False
        for match_str, price_div, qty_mult, new_unit in liquid_conversion_rules:
            if unit_str == match_str.replace(" ", ""):
                price_val = parse_number(row.get("price"))
                qty_val = parse_number(row.get("quantity"))
                if price_val is not None:
                    row["price"] = price_val / price_div
                if qty_val is not None:
                    row["quantity"] = qty_val * qty_mult
                row["unit"] = new_unit
                liquid_matched = True
                break

        if liquid_matched:
            continue

        unit_kg = None
        for match_str, kg in conversion_rules:
            if unit_str == match_str.replace(" ", ""):
                unit_kg = kg
                break
        if unit_kg is None:
            continue

        price_val = parse_number(row.get("price"))
        qty_val = parse_number(row.get("quantity"))
        if price_val is not None:
            row["price"] = price_val / unit_kg * 1000
        if qty_val is not None:
            row["quantity"] = qty_val * unit_kg / 1000
        row["unit"] = "Tonne"

    return kept


def apply_product_mapping(
    row: dict[str, Any], rules: list[tuple[str, str, str]]
) -> None:
    """Set category and farm_description from first keyword match in product description."""
    desc = (row.get("product_description") and str(row["product_description"]).strip()) or ""
    desc_lower = desc.lower()
    for keyword_lower, category, farm_desc in rules:
        if keyword_lower in desc_lower:
            row["category"] = category if category else "Unknown"
            row["farm_description"] = farm_desc if farm_desc else "Unknown"
            return
    row["category"] = "Unknown"
    row["farm_description"] = "Unknown"


def update_credit_flags(rows: list[dict[str, Any]]) -> None:
    """Set credit to Yes when goods value is negative, otherwise No."""
    for row in rows:
        goods_value = parse_number(row.get("goods_value"))
        row["credit"] = "Yes" if is_credit_goods_value(goods_value) else "No"


def update_recent_flags(rows: list[dict[str, Any]]) -> None:
    """Set recent to Yes/No using the same rules as desktop update_recent_column."""
    if not rows:
        return

    max_date: datetime.date | None = None
    for row in rows:
        d = cell_to_date(row.get("invoice_date"))
        if d is not None and (max_date is None or d > max_date):
            max_date = d

    if max_date is None:
        for row in rows:
            row["recent"] = "No"
        return

    recent_month, recent_year = max_date.month, max_date.year

    farm_descs_in_recent_month: set[str] = set()
    for row in rows:
        d = cell_to_date(row.get("invoice_date"))
        if d is not None and d.month == recent_month and d.year == recent_year:
            val = row.get("farm_description")
            if val is not None and str(val).strip():
                farm_descs_in_recent_month.add(str(val).strip().lower())

    for row in rows:
        d = cell_to_date(row.get("invoice_date"))
        in_recent_month = d is not None and d.month == recent_month and d.year == recent_year
        farm_desc = (row.get("farm_description") and str(row["farm_description"]).strip()) or ""
        farm_was_in_recent_month = farm_desc.lower() in farm_descs_in_recent_month
        row["recent"] = "Yes" if (in_recent_month or farm_was_in_recent_month) else "No"


def refresh_all_rows(
    rows: list[dict[str, Any]],
    mapping_rules: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Full refresh: unit transforms, keyword mapping, recent and credit flags."""
    transformed = clean_and_transform_rows(rows)
    for row in transformed:
        apply_product_mapping(row, mapping_rules)
    update_recent_flags(transformed)
    update_credit_flags(transformed)
    return transformed
