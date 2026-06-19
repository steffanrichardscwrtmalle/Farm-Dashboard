"""Business operations: import, refresh, list."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import func, or_, select, and_
from sqlalchemy.orm import Session

from app.models import SUPPLIER_WYNNSTAY, ImportBatch, InvoiceLine, ProductMappingRule
from app.services.invoice_import import parse_import_file
from app.services.mappings import get_product_mapping_rules, seed_mappings_if_empty
from app.services.mapping_options import seed_mapping_options_if_empty
from app.services.transforms import (
    apply_product_mapping,
    clean_and_transform_rows,
    refresh_all_rows,
    update_recent_flags,
    update_credit_flags,
)


def ensure_mappings_seeded(db: Session) -> None:
    seed_mappings_if_empty(db, supplier=SUPPLIER_WYNNSTAY)
    seed_mapping_options_if_empty(db, supplier=SUPPLIER_WYNNSTAY)


def _wynnstay_lines_query():
    return select(InvoiceLine).where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)


def _lines_to_row_dicts(lines: list[InvoiceLine]) -> list[dict[str, Any]]:
    return [
        {
            "business": line.business,
            "date": line.date,
            "reference": line.reference,
            "product_code": line.product_code,
            "category": line.category,
            "product_description": line.product_description,
            "farm_description": line.farm_description,
            "quantity": line.quantity,
            "unit": line.unit,
            "price": line.price,
            "goods_value": line.goods_value,
            "vat": line.vat,
            "total": line.total,
            "date_added": line.date_added,
            "invoice_date": line.invoice_date,
            "recent": line.recent,
            "credit": line.credit,
        }
        for line in lines
    ]


def _apply_refreshed_rows(lines: list[InvoiceLine], refreshed: list[dict[str, Any]]) -> None:
    for line, row in zip(lines, refreshed, strict=True):
        line.apply_dict(row)


def refresh_all_invoice_lines(db: Session) -> int:
    """Re-run unit transforms, keyword map (category + farm), and Recent flags."""
    lines = list(db.scalars(_wynnstay_lines_query().order_by(InvoiceLine.id)).all())
    if not lines:
        return 0

    mapping_rules = get_product_mapping_rules(db, supplier=SUPPLIER_WYNNSTAY)
    row_dicts = _lines_to_row_dicts(lines)
    refreshed = refresh_all_rows(row_dicts, mapping_rules)
    _apply_refreshed_rows(lines, refreshed)
    db.commit()
    return len(lines)


def import_excel_file(
    db: Session,
    file_bytes: bytes,
    filename: str,
    invoice_date: datetime.date,
    business: str,
) -> dict[str, Any]:
    ensure_mappings_seeded(db)

    raw_rows = parse_import_file(file_bytes, invoice_date)
    raw_count = len(raw_rows)

    mapping_rules = get_product_mapping_rules(db, supplier=SUPPLIER_WYNNSTAY)

    transformed = clean_and_transform_rows(raw_rows)
    dropped = raw_count - len(transformed)

    for row in transformed:
        apply_product_mapping(row, mapping_rules)
        row["business"] = business

    existing_lines = list(
        db.scalars(_wynnstay_lines_query().order_by(InvoiceLine.id)).all()
    )
    existing_dicts = _lines_to_row_dicts(existing_lines)
    combined = existing_dicts + transformed
    update_recent_flags(combined)
    update_credit_flags(combined)

    batch = ImportBatch(
        supplier=SUPPLIER_WYNNSTAY,
        source_filename=filename,
        invoice_date=invoice_date,
        rows_imported=len(transformed),
        rows_dropped=dropped,
    )
    db.add(batch)
    db.flush()

    _apply_refreshed_rows(existing_lines, combined[: len(existing_lines)])

    for row in combined[len(existing_lines) :]:
        db.add(
            InvoiceLine.from_row_dict(
                row, import_batch_id=batch.id, supplier=SUPPLIER_WYNNSTAY
            )
        )

    db.commit()

    return {
        "rows_parsed": raw_count,
        "rows_imported": len(transformed),
        "rows_dropped": dropped,
        "batch_id": batch.id,
    }


def _unknown_filter(unknown: str | None):
    if unknown == "category":
        return InvoiceLine.category == "Unknown"
    if unknown == "farm":
        return InvoiceLine.farm_description == "Unknown"
    if unknown == "any":
        return or_(InvoiceLine.category == "Unknown", InvoiceLine.farm_description == "Unknown")
    return None


def _parse_month(value: str) -> tuple[int, int]:
    year_s, month_s = value.split("-", 1)
    year, month = int(year_s), int(month_s)
    if month < 1 or month > 12:
        raise ValueError("Invalid month")
    return year, month


def _month_range_bounds(from_month: str, to_month: str) -> tuple[datetime.date, datetime.date]:
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
        query = query.where(_exclude_credit_clause())
    return {str(farm).strip() for farm in db.scalars(query).all() if farm and str(farm).strip()}


def _credit_filter(credit: str | None):
    if credit == "yes":
        return and_(InvoiceLine.goods_value.isnot(None), InvoiceLine.goods_value < 0)
    if credit == "no":
        return or_(InvoiceLine.goods_value.is_(None), InvoiceLine.goods_value >= 0)
    return None


def _exclude_credit_clause():
    return or_(InvoiceLine.goods_value.is_(None), InvoiceLine.goods_value >= 0)


def list_invoice_lines(
    db: Session,
    *,
    recent: str | None = None,
    recent_only: bool = False,
    unknown: str | None = None,
    invoice_month: str | None = None,
    from_month: str | None = None,
    to_month: str | None = None,
    credit: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[InvoiceLine], int]:
    query = select(InvoiceLine).where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
    count_query = (
        select(func.count())
        .select_from(InvoiceLine)
        .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
    )
    order_by_date = False

    if recent:
        query = query.where(InvoiceLine.recent == recent)
        count_query = count_query.where(InvoiceLine.recent == recent)

    unknown_clause = _unknown_filter(unknown)
    if unknown_clause is not None:
        query = query.where(unknown_clause)
        count_query = count_query.where(unknown_clause)

    credit_clause = _credit_filter(credit)
    if credit_clause is not None:
        query = query.where(credit_clause)
        count_query = count_query.where(credit_clause)

    if from_month and to_month:
        try:
            start, end = _month_range_bounds(from_month, to_month)
            month_clause = (InvoiceLine.invoice_date >= start) & (InvoiceLine.invoice_date < end)
            query = query.where(month_clause)
            count_query = count_query.where(month_clause)
            order_by_date = True
        except (ValueError, TypeError):
            pass
    elif invoice_month:
        try:
            year_s, month_s = invoice_month.split("-", 1)
            year, month = int(year_s), int(month_s)
            start = datetime.date(year, month, 1)
            if month == 12:
                end = datetime.date(year + 1, 1, 1)
            else:
                end = datetime.date(year, month + 1, 1)
            month_clause = (InvoiceLine.invoice_date >= start) & (InvoiceLine.invoice_date < end)
            query = query.where(month_clause)
            count_query = count_query.where(month_clause)
            order_by_date = True
        except (ValueError, TypeError):
            pass

    if recent_only:
        include_credit_for_recent = credit != "no"
        recent_farms = _recent_farm_names(db, include_credit=include_credit_for_recent)
        if not recent_farms:
            return [], 0
        farm_clause = InvoiceLine.farm_description.in_(recent_farms)
        query = query.where(farm_clause)
        count_query = count_query.where(farm_clause)

    if order_by_date:
        query = query.order_by(InvoiceLine.invoice_date.asc(), InvoiceLine.id.asc())
    else:
        query = query.order_by(InvoiceLine.id.desc())

    total = db.scalar(count_query) or 0
    lines = list(db.scalars(query.offset(offset).limit(limit)).all())
    return lines, total


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_invoice_month_label(year: int, month: int) -> str:
    yy = year % 100
    return f"{_MONTH_ABBR[month - 1]}-{yy:02d}"


def get_invoice_months(db: Session) -> list[dict[str, str]]:
    """Distinct invoice_date year/month pairs, chronological order."""
    dates = db.scalars(
        select(InvoiceLine.invoice_date)
        .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
        .where(InvoiceLine.invoice_date.isnot(None))
        .distinct()
        .order_by(InvoiceLine.invoice_date)
    ).all()

    seen: set[str] = set()
    months: list[dict[str, str]] = []
    for d in dates:
        value = f"{d.year}-{d.month:02d}"
        if value in seen:
            continue
        seen.add(value)
        months.append({"value": value, "label": format_invoice_month_label(d.year, d.month)})

    return months

def get_unknown_products(db: Session) -> list[dict[str, Any]]:
    """Distinct product descriptions where category or farm is Unknown."""
    lines = list(
        db.scalars(
            select(InvoiceLine).where(
                InvoiceLine.supplier == SUPPLIER_WYNNSTAY,
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


def get_stats(db: Session) -> dict[str, Any]:
    total_lines = (
        db.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY)
        )
        or 0
    )
    recent_yes = (
        db.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(InvoiceLine.supplier == SUPPLIER_WYNNSTAY, InvoiceLine.recent == "Yes")
        )
        or 0
    )
    unknown_count = (
        db.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(
                InvoiceLine.supplier == SUPPLIER_WYNNSTAY,
                or_(InvoiceLine.category == "Unknown", InvoiceLine.farm_description == "Unknown"),
            )
        )
        or 0
    )
    mapping_count = (
        db.scalar(
            select(func.count())
            .select_from(ProductMappingRule)
            .where(ProductMappingRule.supplier == SUPPLIER_WYNNSTAY)
        )
        or 0
    )

    last_batch = db.scalar(
        select(ImportBatch)
        .where(ImportBatch.supplier == SUPPLIER_WYNNSTAY)
        .order_by(ImportBatch.id.desc())
        .limit(1)
    )
    max_invoice_date = db.scalar(
        select(func.max(InvoiceLine.invoice_date)).where(
            InvoiceLine.supplier == SUPPLIER_WYNNSTAY
        )
    )

    return {
        "total_lines": total_lines,
        "recent_yes": recent_yes,
        "unknown_lines": unknown_count,
        "mapping_rules": mapping_count,
        "last_import": {
            "filename": last_batch.source_filename,
            "rows_imported": last_batch.rows_imported,
            "created_at": last_batch.created_at.isoformat() if last_batch.created_at else None,
        }
        if last_batch
        else None,
        "latest_invoice_date": max_invoice_date.isoformat() if max_invoice_date else None,
    }
