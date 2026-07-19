"""Normalise Xero line amounts to ex-VAT for P&L / Actual Data (match Xero reports)."""

from __future__ import annotations

_AMOUNT_EPS = 0.05


def normalize_line_amount_types(value: str | None) -> str | None:
    text = (value or "").strip().upper()
    return text or None


def document_is_inclusive(
    line_amount_types: str | None,
    *,
    sub_total: float | None = None,
    total: float | None = None,
    line_sum: float | None = None,
) -> bool:
    """Whether document LineAmount values include VAT.

    Prefer Xero LineAmountTypes. For legacy rows (types missing), infer Inclusive
    when line amounts sum to Total but not SubTotal.
    """
    types = normalize_line_amount_types(line_amount_types)
    if types == "INCLUSIVE":
        return True
    if types in {"EXCLUSIVE", "NOTAX"}:
        return False
    if (
        total is not None
        and sub_total is not None
        and line_sum is not None
        and abs(float(line_sum) - float(total)) < _AMOUNT_EPS
        and abs(float(line_sum) - float(sub_total)) > _AMOUNT_EPS
    ):
        return True
    return False


def ex_vat_line_amount(
    line_amount: float | None,
    tax_amount: float | None,
    *,
    inclusive: bool,
) -> float:
    amount = float(line_amount or 0.0)
    if not inclusive:
        return amount
    return amount - float(tax_amount or 0.0)
