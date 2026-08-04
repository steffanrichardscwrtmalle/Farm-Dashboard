"""Shared inventory valuation rules (Power Query / DC305 methodology)."""

from __future__ import annotations

from app.services.herd_import_utils import CATEGORY_BEEF, category_from_birth

CATEGORIES: tuple[str, ...] = ("Beef", "Dairy", "Youngstock")
VALUE_CAP = 1800.0

# DairyComp SBRD codes treated as dairy (legacy blank/"H" plus Holstein variants).
_DAIRY_SBRD_CODES = frozenset({"", "H", "HF", "HO", "HOLSTEIN"})


def normalize_inventory_sbrd(sbrd: str | None) -> str:
    """Uppercase raw SBRD code from the inventory file (e.g. HF, AAX, HEX)."""
    if sbrd is None:
        return ""
    text = str(sbrd).strip().upper()
    if text in {"", "NAN", "NONE", "-"}:
        return ""
    return text


def inventory_sbrd_is_beef(sbrd: str | None) -> bool:
    """True when SBRD is a beef code (AA/AAX/HE/HEX/…) or legacy 'Beef' label."""
    return normalize_inventory_sbrd(sbrd) not in _DAIRY_SBRD_CODES


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def category_from_inventory(lact: int | float | None, sbrd: str | None) -> str:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return "Dairy"
    if inventory_sbrd_is_beef(sbrd):
        return "Beef"
    if lact_n == 0:
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
        "adjusted backwards for sales, deaths, births, and purchases. "
        "Each month’s close date is the earlier of calendar month-end and the inventory "
        "anchor date — GAME/PATHWAY after that date do not affect that month’s headcount."
    ),
    "dairy_cows": "Lact 1: £2,500; Lact 2: £2,200; Lact 3+: £1,800",
    "beef": "£100 + £1.90 × age in days (max £1,800)",
    "youngstock": "£100 + £2.50 × age in days (max £1,800)",
    "age": "Age is calculated to each fiscal month-end closing date from birth date.",
    "joint_venture": (
        "Beef animals with a GAME or PATHWAY event on or before the month close are "
        "excluded from valuations (joint venture transfer). Accruals still counts them "
        "until SOLD or DIED; a later sale does not restore them to historical valuations."
    ),
    "headcount": (
        "Headcounts are reconstructed from inventory and events using the same stock-group "
        "rules as Stock Accruals. Beef excludes joint-venture transfers (GAME/PATHWAY) "
        "that accruals still counts — compare to accruals beef minus JV beef. "
        "On the anchor month, close date may be before calendar month-end; small compare "
        "deltas can still appear if the accruals ledger and reconstruction disagree."
    ),
}
