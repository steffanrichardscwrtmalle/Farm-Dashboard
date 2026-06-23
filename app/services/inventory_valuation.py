"""Shared inventory valuation rules (Power Query / DC305 methodology)."""

from __future__ import annotations

from app.services.herd_import_utils import CATEGORY_BEEF, category_from_birth

CATEGORIES: tuple[str, ...] = ("Beef", "Dairy", "Youngstock")
VALUE_CAP = 1800.0


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def category_from_inventory(lact: int | float | None, sbrd: str | None) -> str:
    lact_n = _normalize_lact(lact)
    sbrd_norm = (sbrd or "").strip()
    if lact_n > 0:
        return "Dairy"
    if sbrd_norm == "Beef":
        return "Beef"
    if sbrd_norm == "Holstein" and lact_n == 0:
        return "Youngstock"
    return "Dairy"


def category_from_event_proxy(
    lact: int | float | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return "Dairy"
    if category_from_birth(cbrd, gndr) == CATEGORY_BEEF:
        return "Beef"
    return "Youngstock"


def compute_value(
    lact: int | float | None,
    category: str,
    aged_days: int | float | None,
) -> float:
    lact_n = _normalize_lact(lact)
    if lact_n == 1:
        return 2500.0
    if lact_n == 2:
        return 2200.0
    if lact_n > 2:
        return 1800.0

    aged_val = 0
    if aged_days is not None:
        try:
            aged_val = max(0, int(aged_days))
        except (TypeError, ValueError):
            aged_val = 0

    if category == "Beef":
        base_value = 100 + (1.90 * aged_val)
    elif category == "Youngstock":
        base_value = 100 + (2.5 * aged_val)
    else:
        base_value = 100.0
    return round(min(base_value, VALUE_CAP), 0)


def birth_category_to_stock_category(birth_category: str | None) -> str:
    """Map birth file category to inventory valuation category at lact 0."""
    if (birth_category or "").strip() == CATEGORY_BEEF:
        return "Beef"
    return "Youngstock"


METHODOLOGY_SUMMARY: dict[str, str] = {
    "anchor": (
        "Closing valuations are reconstructed from the latest herd inventory import, "
        "adjusted backwards for sales, deaths, births, and purchases."
    ),
    "dairy_cows": "Lact 1: £2,500; Lact 2: £2,200; Lact 3+: £1,800",
    "beef": "£100 + £1.90 × age in days (max £1,800)",
    "youngstock": "£100 + £2.50 × age in days (max £1,800)",
    "age": "Age is calculated to each fiscal month-end closing date from birth date.",
    "joint_venture": (
        "Beef animals with a GAME or PATHWAY event are excluded from valuations "
        "from that event date onwards (joint venture transfer)."
    ),
}
