"""Shared category → farm → product matrix by invoice month."""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import SUPPLIER_WYNNSTAY, InvoiceLine
from app.services.category_breakdown import month_range_bounds
from app.services.invoice_ops import format_invoice_month_label

ValueField = Literal["avg_price", "quantity", "goods_value"]


def _label(value: str | None) -> str:
    s = (value or "").strip()
    return s if s else "(Blank)"


def _parse_month(value: str) -> tuple[int, int]:
    year_s, month_s = value.split("-", 1)
    year, month = int(year_s), int(month_s)
    if month < 1 or month > 12:
        raise ValueError("Invalid month")
    return year, month


def _month_columns(from_month: str, to_month: str) -> list[dict[str, str]]:
    from_year, from_m = _parse_month(from_month)
    to_year, to_m = _parse_month(to_month)
    columns: list[dict[str, str]] = []
    year, month = from_year, from_m
    while (year, month) <= (to_year, to_m):
        value = f"{year}-{month:02d}"
        columns.append({"value": value, "label": format_invoice_month_label(year, month)})
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return columns


def _line_month_key(line: InvoiceLine) -> str | None:
    if line.invoice_date is None:
        return None
    return f"{line.invoice_date.year}-{line.invoice_date.month:02d}"


def _aggregate_month(lines: list[InvoiceLine]) -> dict[str, Any] | None:
    if not lines:
        return None
    quantity = 0.0
    goods_value = 0.0
    for line in lines:
        if line.quantity is not None:
            quantity += line.quantity
        if line.goods_value is not None:
            goods_value += line.goods_value
    avg_price = goods_value / quantity if quantity else None
    if avg_price is None:
        prices = [line.price for line in lines if line.price is not None]
        avg_price = sum(prices) / len(prices) if prices else None
    return {
        "avg_price": avg_price,
        "quantity": quantity,
        "goods_value": goods_value,
        "line_count": len(lines),
    }


def _month_data_for_months(
    lines: list[InvoiceLine],
    month_values: list[str],
    *,
    value_field: ValueField,
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    by_month: dict[str, list[InvoiceLine]] = defaultdict(list)
    for line in lines:
        key = _line_month_key(line)
        if key:
            by_month[key].append(line)

    values: dict[str, float | None] = {}
    month_stats: dict[str, dict[str, Any]] = {}
    for month in month_values:
        agg = _aggregate_month(by_month.get(month, []))
        if agg:
            if value_field == "avg_price":
                values[month] = agg["avg_price"]
            elif value_field == "quantity":
                values[month] = agg["quantity"]
            else:
                values[month] = agg["goods_value"]
            month_stats[month] = agg
        else:
            values[month] = None
    return values, month_stats


def _make_node(
    node_id: str,
    level: str,
    label: str,
    values: dict[str, float | None],
    month_stats: dict[str, dict[str, Any]],
    *,
    values_key: str,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "level": level,
        "label": label,
        values_key: values,
        "month_stats": month_stats,
        "children": [],
    }


def _recent_farm_names(db: Session, *, include_credit: bool) -> set[str]:
    max_date = db.scalar(
        select(func.max(InvoiceLine.invoice_date)).where(
            InvoiceLine.supplier == SUPPLIER_WYNNSTAY,
            InvoiceLine.invoice_date.isnot(None),
        )
    )
    if max_date is None:
        return set()

    start = datetime.date(max_date.year, max_date.month, 1)
    if max_date.month == 12:
        end = datetime.date(max_date.year + 1, 1, 1)
    else:
        end = datetime.date(max_date.year, max_date.month + 1, 1)

    query = (
        select(InvoiceLine.farm_description)
        .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
        .where(InvoiceLine.invoice_date.isnot(None))
        .where(InvoiceLine.invoice_date >= start)
        .where(InvoiceLine.invoice_date < end)
        .where(InvoiceLine.farm_description.isnot(None))
        .distinct()
    )
    if not include_credit:
        query = query.where(
            or_(InvoiceLine.goods_value.is_(None), InvoiceLine.goods_value >= 0)
        )
    return {str(farm).strip() for farm in db.scalars(query).all() if farm and str(farm).strip()}


def _build_farm_product_nodes(
    lines: list[InvoiceLine],
    month_values: list[str],
    *,
    value_field: ValueField,
    values_key: str,
    id_scope: str = "",
) -> list[dict[str, Any]]:
    scope = f"{id_scope}|" if id_scope else ""
    by_farm: dict[str, dict[str, list[InvoiceLine]]] = defaultdict(lambda: defaultdict(list))
    for line in lines:
        farm = _label(line.farm_description)
        prod = _label(line.product_description)
        by_farm[farm][prod].append(line)

    rows: list[dict[str, Any]] = []
    for farm in sorted(by_farm.keys(), key=str.casefold):
        farm_lines = [line for prods in by_farm[farm].values() for line in prods]
        farm_values, farm_stats = _month_data_for_months(
            farm_lines, month_values, value_field=value_field
        )
        farm_node = _make_node(
            f"{scope}farm:{farm}",
            "farm_description",
            farm,
            farm_values,
            farm_stats,
            values_key=values_key,
        )

        for prod in sorted(by_farm[farm].keys(), key=str.casefold):
            prod_lines = by_farm[farm][prod]
            prod_values, prod_stats = _month_data_for_months(
                prod_lines, month_values, value_field=value_field
            )
            farm_node["children"].append(
                _make_node(
                    f"{scope}prod:{farm}|{prod}",
                    "product_description",
                    prod,
                    prod_values,
                    prod_stats,
                    values_key=values_key,
                )
            )

        rows.append(farm_node)

    return rows


def _category_chart_data(
    lines: list[InvoiceLine],
    month_values: list[str],
) -> dict[str, Any]:
    by_cat_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for line in lines:
        cat = _label(line.category)
        key = _line_month_key(line)
        if key is None or line.goods_value is None:
            continue
        by_cat_month[cat][key] += line.goods_value

    series: list[dict[str, Any]] = []
    for cat in sorted(by_cat_month.keys(), key=str.casefold):
        series.append(
            {
                "category": cat,
                "values": {m: by_cat_month[cat].get(m, 0.0) for m in month_values},
            }
        )
    return {"series": series}


def get_product_month_matrix(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    category: str | None = None,
    recent_only: bool = False,
    include_credit: bool = True,
    value_field: ValueField,
    values_key: str,
) -> dict[str, Any]:
    start, end = month_range_bounds(from_month, to_month)
    month_cols = _month_columns(from_month, to_month)
    month_values = [col["value"] for col in month_cols]

    query = (
        select(InvoiceLine)
        .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
        .where(InvoiceLine.invoice_date.isnot(None))
        .where(InvoiceLine.invoice_date >= start)
        .where(InvoiceLine.invoice_date < end)
    )
    if category:
        query = query.where(InvoiceLine.category == category)
    if not include_credit:
        query = query.where(
            or_(InvoiceLine.goods_value.is_(None), InvoiceLine.goods_value >= 0)
        )

    order = (
        InvoiceLine.category,
        InvoiceLine.farm_description,
        InvoiceLine.product_description,
    )

    if recent_only:
        recent_farms = _recent_farm_names(db, include_credit=include_credit)
        if not recent_farms:
            lines: list[InvoiceLine] = []
        else:
            query = query.where(InvoiceLine.farm_description.in_(recent_farms))
            lines = list(db.scalars(query.order_by(*order)).all())
    else:
        lines = list(db.scalars(query.order_by(*order)).all())

    group_by_category = not category
    if group_by_category:
        by_category: dict[str, list[InvoiceLine]] = defaultdict(list)
        for line in lines:
            by_category[_label(line.category)].append(line)

        rows: list[dict[str, Any]] = []
        for cat in sorted(by_category.keys(), key=str.casefold):
            cat_lines = by_category[cat]
            cat_values, cat_stats = _month_data_for_months(
                cat_lines, month_values, value_field=value_field
            )
            cat_node = _make_node(
                f"cat:{cat}",
                "category",
                cat,
                cat_values,
                cat_stats,
                values_key=values_key,
            )
            cat_node["children"] = _build_farm_product_nodes(
                cat_lines,
                month_values,
                value_field=value_field,
                values_key=values_key,
                id_scope=f"cat:{cat}",
            )
            rows.append(cat_node)
    else:
        rows = _build_farm_product_nodes(
            lines,
            month_values,
            value_field=value_field,
            values_key=values_key,
        )

    from_year, from_m = _parse_month(from_month)
    to_year, to_m = _parse_month(to_month)
    if from_month == to_month:
        period_label = format_invoice_month_label(from_year, from_m)
    else:
        period_label = (
            f"{format_invoice_month_label(from_year, from_m)}"
            f" – {format_invoice_month_label(to_year, to_m)}"
        )

    return {
        "from_month": from_month,
        "to_month": to_month,
        "category": category or "",
        "recent_only": recent_only,
        "include_credit": include_credit,
        "period_label": period_label,
        "group_by_category": group_by_category,
        "months": month_cols,
        "rows": rows,
        "category_chart": _category_chart_data(lines, month_values),
    }
