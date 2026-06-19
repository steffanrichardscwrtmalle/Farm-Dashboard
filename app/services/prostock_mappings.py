"""Seed Prostock product mapping rules from PRODUCT_LIBRARY."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.prostock_product_library import PRODUCT_LIBRARY
from app.models import SUPPLIER_PROSTOCK, ProductMappingRule
from app.services.mapping_options import seed_mapping_options_if_empty
from app.services.mappings import import_rules_to_db


def _library_to_rules() -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    for keyword, (product, group) in PRODUCT_LIBRARY.items():
        rules.append(
            {
                "keyword": keyword,
                "category": group,
                "farm_description": product,
            }
        )
    return rules


def seed_prostock_mappings_if_empty(db: Session) -> int:
    existing = db.scalars(
        select(ProductMappingRule)
        .where(ProductMappingRule.supplier == SUPPLIER_PROSTOCK)
        .limit(1)
    ).first()
    if existing is not None:
        return 0
    rules = _library_to_rules()
    if not rules:
        return 0
    return import_rules_to_db(db, rules, replace=True, supplier=SUPPLIER_PROSTOCK)


def ensure_prostock_mappings_seeded(db: Session) -> None:
    seed_prostock_mappings_if_empty(db)
    seed_mapping_options_if_empty(db, supplier=SUPPLIER_PROSTOCK)


def reseed_prostock_mappings_from_library(db: Session) -> int:
    """Replace all Prostock keyword rules with PRODUCT_LIBRARY."""
    rules = _library_to_rules()
    if not rules:
        return 0
    return import_rules_to_db(db, rules, replace=True, supplier=SUPPLIER_PROSTOCK)
