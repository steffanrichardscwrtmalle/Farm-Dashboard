"""Closing Stock P&L is shown as a gain when Xero posts a credit."""

from app.services.xero_actuals import apply_closing_stock_pnl_sign


def test_closing_stock_credit_is_shown_as_a_gain() -> None:
    assert apply_closing_stock_pnl_sign(-23400.0, "Closing Stock P&L") == 23400.0


def test_closing_stock_debit_is_shown_as_a_loss() -> None:
    assert apply_closing_stock_pnl_sign(18000.0, "closing stock p&l") == -18000.0


def test_other_accounts_keep_their_sign() -> None:
    assert apply_closing_stock_pnl_sign(23400.0, "Farming Stock per Valuation") == 23400.0
    assert apply_closing_stock_pnl_sign(-100.0, "Feed") == -100.0
    assert apply_closing_stock_pnl_sign(50.0, None) == 50.0
