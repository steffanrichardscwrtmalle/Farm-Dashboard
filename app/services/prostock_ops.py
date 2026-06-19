"""Prostock import and refresh operations."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import SUPPLIER_PROSTOCK, PROSTOCK_BUSINESS_OPTIONS, ImportBatch, InvoiceLine
from app.services.invoice_ops import _month_range_bounds, format_invoice_month_label
from app.services.mappings import get_product_mapping_rules
from app.services.prostock_import import parse_prostock_file
from app.services.transforms import apply_product_mapping


def _validate_business(business: str) -> None:
    if business not in PROSTOCK_BUSINESS_OPTIONS:
        raise ValueError(
            f"Business must be one of: {', '.join(PROSTOCK_BUSINESS_OPTIONS)}"
        )


def get_unknown_drugs(db: Session) -> list[dict[str, Any]]:
    lines = list(
        db.scalars(
            select(InvoiceLine).where(
                InvoiceLine.supplier == SUPPLIER_PROSTOCK,
                or_(InvoiceLine.category == "Unknown", InvoiceLine.farm_description == "Unknown"),
            )
        ).all()
    )
    by_desc: dict[str, dict[str, Any]] = {}
    for line in lines:
        desc = (line.product_description or "").strip()
        if not desc:
            continue
        if desc not in by_desc:
            by_desc[desc] = {
                "product_description": desc,
                "line_count": 0,
                "category": line.category,
                "farm_description": line.farm_description,
            }
        by_desc[desc]["line_count"] += 1
    return sorted(by_desc.values(), key=lambda x: (-x["line_count"], x["product_description"]))


def refresh_prostock_lines(db: Session, business: str | None = None) -> int:
    query = select(InvoiceLine).where(InvoiceLine.supplier == SUPPLIER_PROSTOCK)
    if business:
        query = query.where(InvoiceLine.business == business)
    lines = list(db.scalars(query.order_by(InvoiceLine.id)).all())
    if not lines:
        return 0

    rules = get_product_mapping_rules(db, supplier=SUPPLIER_PROSTOCK)
    for line in lines:
        row = {"product_description": line.product_description}
        apply_product_mapping(row, rules)
        line.category = row.get("category")
        line.farm_description = row.get("farm_description")

    db.commit()
    return len(lines)


def import_prostock_file(
    db: Session,
    file_bytes: bytes,
    filename: str,
    business: str,
) -> dict[str, Any]:
    _validate_business(business)

    from app.services.prostock_mappings import ensure_prostock_mappings_seeded

    ensure_prostock_mappings_seeded(db)

    parsed = parse_prostock_file(file_bytes)
    if not parsed:
        raise ValueError("No data rows found in the uploaded file")

    rules = get_product_mapping_rules(db, supplier=SUPPLIER_PROSTOCK)
    rows: list[dict[str, Any]] = []
    for item in parsed:
        row = dict(item)
        apply_product_mapping(row, rules)
        row["business"] = business
        rows.append(row)

    db.execute(
        delete(InvoiceLine).where(
            InvoiceLine.supplier == SUPPLIER_PROSTOCK,
            InvoiceLine.business == business,
        )
    )

    invoice_dates = [r["invoice_date"] for r in rows if r.get("invoice_date")]
    batch_date = max(invoice_dates) if invoice_dates else datetime.date.today()

    batch = ImportBatch(
        supplier=SUPPLIER_PROSTOCK,
        source_filename=filename,
        invoice_date=batch_date,
        rows_imported=len(rows),
        rows_dropped=0,
    )
    db.add(batch)
    db.flush()

    for row in rows:
        db.add(
            InvoiceLine.from_row_dict(
                row,
                import_batch_id=batch.id,
                supplier=SUPPLIER_PROSTOCK,
            )
        )

    db.commit()

    return {
        "rows_parsed": len(parsed),
        "rows_imported": len(rows),
        "rows_dropped": 0,
        "batch_id": batch.id,
        "business": business,
    }


def get_prostock_stats(db: Session, business: str | None = None) -> dict[str, Any]:
    query = select(func.count()).select_from(InvoiceLine).where(
        InvoiceLine.supplier == SUPPLIER_PROSTOCK
    )
    if business:
        query = query.where(InvoiceLine.business == business)
    total = db.scalar(query) or 0
    return {"total_lines": total, "business": business}


def _normalize_business_filter(businesses: list[str] | None) -> list[str]:
    if not businesses:
        return []
    return [b for b in businesses if b in PROSTOCK_BUSINESS_OPTIONS]


def get_prostock_invoice_months(
    db: Session, *, businesses: list[str] | None = None
) -> list[dict[str, str]]:
    """Distinct invoice months for Prostock, optionally scoped to selected farms."""
    selected = _normalize_business_filter(businesses)
    if not selected:
        return []

    query = (
        select(InvoiceLine.invoice_date)
        .where(InvoiceLine.supplier == SUPPLIER_PROSTOCK)
        .where(InvoiceLine.invoice_date.isnot(None))
        .where(InvoiceLine.business.in_(selected))
        .distinct()
        .order_by(InvoiceLine.invoice_date)
    )
    dates = db.scalars(query).all()

    seen: set[str] = set()
    months: list[dict[str, str]] = []
    for d in dates:
        value = f"{d.year}-{d.month:02d}"
        if value in seen:
            continue
        seen.add(value)
        months.append({"value": value, "label": format_invoice_month_label(d.year, d.month)})
    return months


def list_prostock_invoice_lines(
    db: Session,
    *,
    businesses: list[str] | None = None,
    from_month: str | None = None,
    to_month: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[InvoiceLine], int]:
    selected = _normalize_business_filter(businesses)
    if not selected:
        return [], 0

    query = select(InvoiceLine).where(
        InvoiceLine.supplier == SUPPLIER_PROSTOCK,
        InvoiceLine.business.in_(selected),
    )
    count_query = (
        select(func.count())
        .select_from(InvoiceLine)
        .where(
            InvoiceLine.supplier == SUPPLIER_PROSTOCK,
            InvoiceLine.business.in_(selected),
        )
    )
    order_by_date = False

    if from_month and to_month:
        try:
            start, end = _month_range_bounds(from_month, to_month)
            month_clause = (InvoiceLine.invoice_date >= start) & (InvoiceLine.invoice_date < end)
            query = query.where(month_clause)
            count_query = count_query.where(month_clause)
            order_by_date = True
        except (ValueError, TypeError):
            pass

    if order_by_date:
        query = query.order_by(
            InvoiceLine.invoice_date.asc(),
            InvoiceLine.business.asc(),
            InvoiceLine.id.asc(),
        )
    else:
        query = query.order_by(InvoiceLine.id.desc())

    total = db.scalar(count_query) or 0
    lines = list(db.scalars(query.offset(offset).limit(limit)).all())
    return lines, total
