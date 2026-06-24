"""Shared stock-group classification (aligned with Stock Accruals event filters)."""

from __future__ import annotations

from app.models import STOCK_GROUP_BEEF, STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK
from app.services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_BEEF

VALUATION_CATEGORY_BY_STOCK_GROUP: dict[str, str] = {
    STOCK_GROUP_COWS: "Dairy",
    STOCK_GROUP_YOUNGSTOCK: "Youngstock",
    STOCK_GROUP_BEEF: "Beef",
}


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def _normalize_cbrd(cbrd: int | float | None) -> int | None:
    if cbrd is None:
        return None
    try:
        return int(cbrd)
    except (TypeError, ValueError):
        return None


def stock_group_from_event_fields(
    lact: int | float | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    """Mirror ``_apply_cow_event_stock_group`` in stock_accruals."""
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return STOCK_GROUP_COWS

    gender = (gndr or "").strip().upper()
    cbrd_code = _normalize_cbrd(cbrd)
    if gender == "F" and cbrd_code is not None and cbrd_code < BEEF_CBREED_MIN:
        return STOCK_GROUP_YOUNGSTOCK
    return STOCK_GROUP_BEEF


def stock_group_from_inventory(lact: int | float | None, sbrd: str | None) -> str:
    """Map inventory lact/sbrd to accruals stock group at lact 0."""
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return STOCK_GROUP_COWS
    sbrd_norm = (sbrd or "").strip()
    if sbrd_norm == "Beef":
        return STOCK_GROUP_BEEF
    if sbrd_norm == "Holstein":
        return STOCK_GROUP_YOUNGSTOCK
    return STOCK_GROUP_BEEF


def stock_group_from_birth(
    birth_category: str | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    if (birth_category or "").strip() == CATEGORY_BEEF:
        return STOCK_GROUP_BEEF
    return stock_group_from_event_fields(0, cbrd, gndr)


def valuation_category_from_stock_group(stock_group: str) -> str:
    return VALUATION_CATEGORY_BY_STOCK_GROUP[stock_group]
