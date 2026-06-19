"""Hierarchical category breakdown (pivot-style) for invoice lines."""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import SUPPLIER_WYNNSTAY, InvoiceLine
from app.services.invoice_ops import format_invoice_month_label


def _label(value: str | None) -> str:
    s = (value or "").strip()
    return s if s else "(Blank)"


def _parse_month(value: str) -> tuple[int, int]:
    year_s, month_s = value.split("-", 1)
    year, month = int(year_s), int(month_s)
    if month < 1 or month > 12:
        raise ValueError("Invalid month")
    return year, month


def month_range_bounds(from_month: str, to_month: str) -> tuple[datetime.date, datetime.date]:
    from_year, from_m = _parse_month(from_month)
    to_year, to_m = _parse_month(to_month)
    if (to_year, to_m) < (from_year, from_m):
        raise ValueError("to_month must not be before from_month")

    start = datetime.date(from_year, from_m, 1)
    if to_m == 12:
        end = datetime.date(to_year + 1, 1, 1)
    else:
        end = datetime.date(to_year, to_m + 1, 1)
    return start, end


def _aggregate(lines: list[InvoiceLine]) -> dict[str, Any]:
    quantity = 0.0
    goods_value = 0.0
    vat = 0.0
    total = 0.0
    for line in lines:
        if line.quantity is not None:
            quantity += line.quantity
        if line.goods_value is not None:
            goods_value += line.goods_value
        if line.vat is not None:
            vat += line.vat
        if line.total is not None:
            total += line.total
    avg_price = goods_value / quantity if quantity else None
    return {
        "quantity": quantity,
        "avg_price": avg_price,
        "goods_value": goods_value,
        "vat": vat,
        "total": total,
        "line_count": len(lines),
    }


def _make_node(node_id: str, level: str, label: str, totals: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "level": level,
        "label": label,
        "totals": totals,
        "children": [],
    }


def get_category_breakdown(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    include_credit: bool = True,
) -> dict[str, Any]:
    start, end = month_range_bounds(from_month, to_month)
    query = (
        select(InvoiceLine)
        .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
        .where(InvoiceLine.invoice_date.isnot(None))
        .where(InvoiceLine.invoice_date >= start)
        .where(InvoiceLine.invoice_date < end)
    )
    if not include_credit:
        query = query.where(
            or_(InvoiceLine.goods_value.is_(None), InvoiceLine.goods_value >= 0)
        )
    lines = list(
        db.scalars(
            query.order_by(
                InvoiceLine.category,
                InvoiceLine.farm_description,
                InvoiceLine.product_description,
            )
        ).all()
    )

    by_category: dict[str, dict[str, dict[str, list[InvoiceLine]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for line in lines:
        cat = _label(line.category)
        farm = _label(line.farm_description)
        prod = _label(line.product_description)
        by_category[cat][farm][prod].append(line)

    rows: list[dict[str, Any]] = []
    for cat in sorted(by_category.keys(), key=str.casefold):
        cat_lines = [
            line
            for farms in by_category[cat].values()
            for prods in farms.values()
            for line in prods
        ]
        cat_node = _make_node(f"cat:{cat}", "category", cat, _aggregate(cat_lines))

        for farm in sorted(by_category[cat].keys(), key=str.casefold):
            farm_lines = [line for prods in by_category[cat][farm].values() for line in prods]
            farm_node = _make_node(
                f"farm:{cat}|{farm}",
                "farm_description",
                farm,
                _aggregate(farm_lines),
            )

            for prod in sorted(by_category[cat][farm].keys(), key=str.casefold):
                prod_lines = by_category[cat][farm][prod]
                farm_node["children"].append(
                    _make_node(
                        f"prod:{cat}|{farm}|{prod}",
                        "product_description",
                        prod,
                        _aggregate(prod_lines),
                    )
                )

            cat_node["children"].append(farm_node)

        rows.append(cat_node)

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
        "include_credit": include_credit,
        "period_label": period_label,
        "grand_total": _aggregate(lines),
        "rows": rows,
    }
