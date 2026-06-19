"""Prostock product matrix: Group → Product → Drug, columns Farm → Month."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PROSTOCK_BUSINESS_OPTIONS, SUPPLIER_PROSTOCK, InvoiceLine
from app.services.category_breakdown import month_range_bounds
from app.services.invoice_ops import format_invoice_month_label
from app.services.prostock_ops import _normalize_business_filter
from app.services.product_month_matrix import _aggregate_month, _label, _line_month_key, _month_columns

ValueField = Literal["avg_price", "quantity", "goods_value"]
VALUES_KEY: dict[ValueField, str] = {
    "avg_price": "prices",
    "quantity": "quantities",
    "goods_value": "spend",
}


def _col_key(business: str, month: str | None = None) -> str:
    if month:
        return f"{business}|{month}"
    return f"{business}|__all__"


def _value_from_agg(agg: dict[str, Any] | None, value_field: ValueField) -> float | None:
    if not agg:
        return None
    if value_field == "quantity":
        return agg["quantity"]
    if value_field == "goods_value":
        return agg["goods_value"]
    return agg["avg_price"]


def _meaningful_agg(lines: list[InvoiceLine]) -> dict[str, Any] | None:
    """Aggregate lines; return None when there is no quantity or amount in the period."""
    agg = _aggregate_month(lines)
    if not agg:
        return None
    quantity = agg.get("quantity") or 0
    goods_value = agg.get("goods_value") or 0
    if quantity == 0 and goods_value == 0:
        return None
    return agg


def _stats_for_lines(
    lines: list[InvoiceLine],
    businesses: list[str],
    month_values: list[str],
    *,
    value_field: ValueField,
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    values: dict[str, float | None] = {}
    stats: dict[str, dict[str, Any]] = {}

    for business in businesses:
        biz_lines = [line for line in lines if (line.business or "").strip() == business]
        rollup = _meaningful_agg(biz_lines)
        rollup_key = _col_key(business)
        if rollup:
            values[rollup_key] = _value_from_agg(rollup, value_field)
            stats[rollup_key] = rollup
        else:
            values[rollup_key] = None

        for month in month_values:
            month_lines = [
                line for line in biz_lines if _line_month_key(line) == month
            ]
            key = _col_key(business, month)
            agg = _meaningful_agg(month_lines)
            if agg:
                values[key] = _value_from_agg(agg, value_field)
                stats[key] = agg
            else:
                values[key] = None

    return values, stats


def _make_node(
    node_id: str,
    level: str,
    label: str,
    values: dict[str, float | None],
    cell_stats: dict[str, dict[str, Any]],
    *,
    values_key: str,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "level": level,
        "label": label,
        values_key: values,
        "cell_stats": cell_stats,
        "children": [],
    }


def _build_drug_nodes(
    lines: list[InvoiceLine],
    businesses: list[str],
    month_values: list[str],
    *,
    id_scope: str,
    value_field: ValueField,
    values_key: str,
) -> list[dict[str, Any]]:
    by_product: dict[str, dict[str, list[InvoiceLine]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for line in lines:
        product = _label(line.farm_description)
        drug = _label(line.product_description)
        by_product[product][drug].append(line)

    rows: list[dict[str, Any]] = []
    for product in sorted(by_product.keys(), key=str.casefold):
        product_lines = [
            line for drugs in by_product[product].values() for line in drugs
        ]
        product_values, product_stats = _stats_for_lines(
            product_lines, businesses, month_values, value_field=value_field
        )
        product_node = _make_node(
            f"{id_scope}|prod:{product}",
            "farm_description",
            product,
            product_values,
            product_stats,
            values_key=values_key,
        )

        for drug in sorted(by_product[product].keys(), key=str.casefold):
            drug_lines = by_product[product][drug]
            drug_values, drug_stats = _stats_for_lines(
                drug_lines, businesses, month_values, value_field=value_field
            )
            product_node["children"].append(
                _make_node(
                    f"{id_scope}|drug:{product}|{drug}",
                    "product_description",
                    drug,
                    drug_values,
                    drug_stats,
                    values_key=values_key,
                )
            )

        rows.append(product_node)

    return rows


def _node_has_data(node: dict[str, Any]) -> bool:
    """True when the row has quantity or amount in at least one farm/month cell."""
    return bool(node.get("cell_stats"))


def _prune_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop drug rows with no data and parents with no remaining children."""
    pruned: list[dict[str, Any]] = []
    for node in nodes:
        children = node.get("children", [])
        if children:
            node["children"] = _prune_tree(children)
            if node["children"]:
                pruned.append(node)
        elif _node_has_data(node):
            pruned.append(node)
    return pruned


def _group_chart_data(
    lines: list[InvoiceLine],
    month_values: list[str],
) -> dict[str, Any]:
    """Spend by group (category) per month for the stacked chart."""
    by_group_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for line in lines:
        group = _label(line.category)
        key = _line_month_key(line)
        if key is None or line.goods_value is None:
            continue
        by_group_month[group][key] += line.goods_value

    series: list[dict[str, Any]] = []
    for group in sorted(by_group_month.keys(), key=str.casefold):
        series.append(
            {
                "category": group,
                "values": {m: by_group_month[group].get(m, 0.0) for m in month_values},
            }
        )
    return {"series": series}


def get_prostock_product_matrix(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    businesses: list[str] | None = None,
    value_field: ValueField = "avg_price",
) -> dict[str, Any]:
    values_key = VALUES_KEY[value_field]
    selected = _normalize_business_filter(businesses)
    if not selected:
        selected = list(PROSTOCK_BUSINESS_OPTIONS)

    start, end = month_range_bounds(from_month, to_month)
    month_cols = _month_columns(from_month, to_month)
    month_values = [col["value"] for col in month_cols]

    lines = list(
        db.scalars(
            select(InvoiceLine)
            .where(InvoiceLine.supplier == SUPPLIER_PROSTOCK)
            .where(InvoiceLine.invoice_date.isnot(None))
            .where(InvoiceLine.invoice_date >= start)
            .where(InvoiceLine.invoice_date < end)
            .where(InvoiceLine.business.in_(selected))
            .order_by(
                InvoiceLine.category,
                InvoiceLine.farm_description,
                InvoiceLine.product_description,
            )
        ).all()
    )

    by_group: dict[str, list[InvoiceLine]] = defaultdict(list)
    for line in lines:
        by_group[_label(line.category)].append(line)

    rows: list[dict[str, Any]] = []
    for group in sorted(by_group.keys(), key=str.casefold):
        group_lines = by_group[group]
        group_values, group_stats = _stats_for_lines(
            group_lines, selected, month_values, value_field=value_field
        )
        group_node = _make_node(
            f"grp:{group}",
            "category",
            group,
            group_values,
            group_stats,
            values_key=values_key,
        )
        group_node["children"] = _build_drug_nodes(
            group_lines,
            selected,
            month_values,
            id_scope=f"grp:{group}",
            value_field=value_field,
            values_key=values_key,
        )
        rows.append(group_node)

    rows = _prune_tree(rows)

    from_year, from_m = map(int, from_month.split("-"))
    to_year, to_m = map(int, to_month.split("-"))
    if from_month == to_month:
        period_label = format_invoice_month_label(from_year, from_m)
    else:
        period_label = (
            f"{format_invoice_month_label(from_year, from_m)}"
            f" – {format_invoice_month_label(to_year, to_m)}"
        )

    column_groups = [
        {
            "id": f"biz:{business}",
            "business": business,
            "label": business,
            "months": month_cols,
        }
        for business in selected
    ]

    return {
        "from_month": from_month,
        "to_month": to_month,
        "businesses": selected,
        "period_label": period_label,
        "months": month_cols,
        "column_groups": column_groups,
        "values_key": values_key,
        "rows": rows,
        "group_chart": _group_chart_data(lines, month_values)
        if value_field == "goods_value"
        else None,
    }


def get_prostock_product_prices(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    businesses: list[str] | None = None,
) -> dict[str, Any]:
    return get_prostock_product_matrix(
        db,
        from_month=from_month,
        to_month=to_month,
        businesses=businesses,
        value_field="avg_price",
    )


def get_prostock_product_quantities(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    businesses: list[str] | None = None,
) -> dict[str, Any]:
    return get_prostock_product_matrix(
        db,
        from_month=from_month,
        to_month=to_month,
        businesses=businesses,
        value_field="quantity",
    )


def get_prostock_monthly_spend(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    businesses: list[str] | None = None,
) -> dict[str, Any]:
    return get_prostock_product_matrix(
        db,
        from_month=from_month,
        to_month=to_month,
        businesses=businesses,
        value_field="goods_value",
    )
