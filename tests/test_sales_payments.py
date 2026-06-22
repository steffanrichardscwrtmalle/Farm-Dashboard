from app.services.events_common import SALES_TABLE_REASON_ORDER
from app.services.sales_payments import normalize_sales_reasons


def test_normalize_sales_reasons_all_selected_includes_beef() -> None:
    selected = normalize_sales_reasons(["CULL", "TB", "OFS", "Beef", "Dairy"])
    assert selected == list(SALES_TABLE_REASON_ORDER)


def test_normalize_sales_reasons_beef_only() -> None:
    assert normalize_sales_reasons(["Beef"]) == ["Beef"]


def test_normalize_sales_reasons_empty_defaults_to_all() -> None:
    assert normalize_sales_reasons(None) == list(SALES_TABLE_REASON_ORDER)
