from app.services.stock_purchases import normalize_stock_group


def test_normalize_stock_group_accepts_beef() -> None:
    assert normalize_stock_group("beef") == "beef"


def test_normalize_stock_group_defaults_unknown_to_cows() -> None:
    assert normalize_stock_group("invalid") == "cows"
