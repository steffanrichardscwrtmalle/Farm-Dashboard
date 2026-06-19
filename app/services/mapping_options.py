"""Managed category and farm description options for keyword rules."""

from __future__ import annotations

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from app.models import SUPPLIER_WYNNSTAY, InvoiceLine, MappingOption, ProductMappingRule

OPTION_CATEGORY = "category"
OPTION_FARM = "farm_description"
VALID_OPTION_TYPES = (OPTION_CATEGORY, OPTION_FARM)


def _normalize(value: str) -> str:
    return value.strip()


def _option_exists(db: Session, option_type: str, value: str, *, supplier: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    existing = db.scalars(
        select(MappingOption).where(
            MappingOption.supplier == supplier,
            MappingOption.option_type == option_type,
            func.lower(MappingOption.value) == normalized.lower(),
        )
    ).first()
    return existing is not None


def add_mapping_option(
    db: Session, option_type: str, value: str, *, supplier: str = SUPPLIER_WYNNSTAY
) -> MappingOption:
    if option_type not in VALID_OPTION_TYPES:
        raise ValueError(f"option_type must be one of: {', '.join(VALID_OPTION_TYPES)}")

    normalized = _normalize(value)
    if not normalized:
        raise ValueError("Value is required")

    existing = db.scalars(
        select(MappingOption).where(
            MappingOption.supplier == supplier,
            MappingOption.option_type == option_type,
            func.lower(MappingOption.value) == normalized.lower(),
        )
    ).first()
    if existing:
        return existing

    option = MappingOption(supplier=supplier, option_type=option_type, value=normalized)
    db.add(option)
    db.commit()
    db.refresh(option)
    return option


def ensure_mapping_option(
    db: Session,
    option_type: str,
    value: str | None,
    *,
    supplier: str = SUPPLIER_WYNNSTAY,
) -> None:
    normalized = _normalize(value or "")
    if not normalized:
        return
    if not _option_exists(db, option_type, normalized, supplier=supplier):
        db.add(MappingOption(supplier=supplier, option_type=option_type, value=normalized))
        db.flush()


def sync_options_from_values(
    db: Session,
    categories: set[str],
    farm_descriptions: set[str],
    *,
    supplier: str = SUPPLIER_WYNNSTAY,
) -> int:
    added = 0
    for value in sorted(categories):
        if value and not _option_exists(db, OPTION_CATEGORY, value, supplier=supplier):
            db.add(MappingOption(supplier=supplier, option_type=OPTION_CATEGORY, value=value))
            added += 1
    for value in sorted(farm_descriptions):
        if value and not _option_exists(db, OPTION_FARM, value, supplier=supplier):
            db.add(MappingOption(supplier=supplier, option_type=OPTION_FARM, value=value))
            added += 1
    if added:
        db.commit()
    return added


def _collect_distinct_values(
    db: Session, *, supplier: str = SUPPLIER_WYNNSTAY
) -> tuple[set[str], set[str]]:
    categories: set[str] = set()
    farm_descriptions: set[str] = set()

    for value in db.scalars(
        select(distinct(ProductMappingRule.category)).where(
            ProductMappingRule.supplier == supplier
        )
    ).all():
        if value and str(value).strip():
            categories.add(str(value).strip())
    for value in db.scalars(
        select(distinct(ProductMappingRule.farm_description)).where(
            ProductMappingRule.supplier == supplier
        )
    ).all():
        if value and str(value).strip():
            farm_descriptions.add(str(value).strip())
    for value in db.scalars(
        select(distinct(InvoiceLine.category)).where(InvoiceLine.supplier == supplier)
    ).all():
        if value and str(value).strip():
            categories.add(str(value).strip())
    for value in db.scalars(
        select(distinct(InvoiceLine.farm_description)).where(InvoiceLine.supplier == supplier)
    ).all():
        if value and str(value).strip():
            farm_descriptions.add(str(value).strip())

    return categories, farm_descriptions


def seed_mapping_options_if_empty(
    db: Session, *, supplier: str = SUPPLIER_WYNNSTAY
) -> int:
    existing = db.scalars(
        select(MappingOption).where(MappingOption.supplier == supplier).limit(1)
    ).first()
    if existing is not None:
        return 0
    categories, farm_descriptions = _collect_distinct_values(db, supplier=supplier)
    return sync_options_from_values(
        db, categories, farm_descriptions, supplier=supplier
    )


def list_mapping_options(
    db: Session, *, supplier: str = SUPPLIER_WYNNSTAY
) -> dict[str, list[str]]:
    options = db.scalars(
        select(MappingOption)
        .where(MappingOption.supplier == supplier)
        .order_by(MappingOption.option_type, MappingOption.value)
    ).all()
    categories: list[str] = []
    farm_descriptions: list[str] = []
    for option in options:
        if option.option_type == OPTION_CATEGORY:
            categories.append(option.value)
        elif option.option_type == OPTION_FARM:
            farm_descriptions.append(option.value)
    return {"categories": categories, "farm_descriptions": farm_descriptions}


def list_mapping_option_rows(
    db: Session, *, supplier: str = SUPPLIER_WYNNSTAY
) -> list[MappingOption]:
    return list(
        db.scalars(
            select(MappingOption)
            .where(MappingOption.supplier == supplier)
            .order_by(MappingOption.option_type, MappingOption.value)
        ).all()
    )


def validate_mapping_values(
    db: Session,
    category: str,
    farm_description: str,
    *,
    supplier: str = SUPPLIER_WYNNSTAY,
) -> None:
    category = _normalize(category)
    farm_description = _normalize(farm_description)

    if category and not _option_exists(db, OPTION_CATEGORY, category, supplier=supplier):
        raise ValueError(f"Category '{category}' is not in the allowed list")
    if farm_description and not _option_exists(db, OPTION_FARM, farm_description, supplier=supplier):
        raise ValueError(f"Farm description '{farm_description}' is not in the allowed list")


def delete_mapping_option(db: Session, option_id: int, *, supplier: str = SUPPLIER_WYNNSTAY) -> None:
    option = db.get(MappingOption, option_id)
    if option is None or option.supplier != supplier:
        raise ValueError("Option not found")

    if option.option_type == OPTION_CATEGORY:
        in_use = db.scalar(
            select(func.count())
            .select_from(ProductMappingRule)
            .where(
                ProductMappingRule.supplier == supplier,
                ProductMappingRule.category == option.value,
            )
        ) or 0
        in_use += db.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(
                InvoiceLine.supplier == supplier,
                InvoiceLine.category == option.value,
            )
        ) or 0
    else:
        in_use = db.scalar(
            select(func.count())
            .select_from(ProductMappingRule)
            .where(
                ProductMappingRule.supplier == supplier,
                ProductMappingRule.farm_description == option.value,
            )
        ) or 0
        in_use += db.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(
                InvoiceLine.supplier == supplier,
                InvoiceLine.farm_description == option.value,
            )
        ) or 0

    if in_use:
        raise ValueError("Cannot delete — this value is still used by invoice lines or keyword rules")

    db.delete(option)
    db.commit()
