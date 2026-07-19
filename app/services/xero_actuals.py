"""Monthly actual sales/costs pivoted by Xero account category."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BUSINESS_GROUP_OPTIONS,
    BUSINESS_OPTIONS,
    XeroAccount,
    XeroBankTransaction,
    XeroBankTransactionLine,
    XeroInvoice,
    XeroInvoiceLine,
    XeroManualJournal,
    XeroManualJournalLine,
)
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
)
from app.services.xero_accounts import account_meta_lookup
from app.services.xero_bank_transactions import (
    BANK_STATUSES,
    bank_type_as_invoice_type,
    is_pnl_bank_type,
)
from app.services.xero_invoices import SUMMARY_STATUSES
from app.services.xero_journals import JOURNAL_STATUSES

_CODE_PARTS_RE = re.compile(r"(\d+|\D+)")
_REVENUE_CLASSES = frozenset({"REVENUE"})
_EXPENSE_CLASSES = frozenset({"EXPENSE"})
_BALANCE_CLASSES = frozenset({"ASSET", "LIABILITY", "EQUITY"})
_SECTION_SALES = "sales"
_SECTION_COSTS = "costs"
_SECTION_BALANCE = "balance_sheet"
_CLASS_LABELS = {
    "REVENUE": "Revenue",
    "EXPENSE": "Expense",
    "ASSET": "Asset",
    "LIABILITY": "Liability",
    "EQUITY": "Equity",
}
_TYPE_LABELS = {
    "REVENUE": "Revenue",
    "SALES": "Sales",
    "OTHERINCOME": "Other income",
    "DIRECTCOSTS": "Direct costs",
    "EXPENSE": "Expense",
    "OVERHEADS": "Overheads",
    "DEPRECIATN": "Depreciation",
    "BANK": "Bank",
    "CURRENT": "Current asset",
    "CURRLIAB": "Current liability",
    "FIXED": "Fixed asset",
    "INVENTORY": "Inventory",
    "NONCURRENT": "Non-current asset",
    "TERMLIAB": "Non-current liability",
    "EQUITY": "Equity",
}


def _month_label(value: dt.date) -> str:
    return value.strftime("%b-%y")


def _code_sort_key(code: str) -> tuple:
    parts: list[tuple[int, object]] = []
    for part in _CODE_PARTS_RE.findall(code or ""):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part.lower()))
    return tuple(parts) if parts else ((1, ""),)


def _class_sort_key(account_class: str | None) -> tuple:
    order = {"REVENUE": 0, "EXPENSE": 1, "ASSET": 2, "LIABILITY": 3, "EQUITY": 4}
    return (order.get(account_class or "", 9), account_class or "")


def _pretty_class(account_class: str | None) -> str:
    if not account_class:
        return "Unknown"
    return _CLASS_LABELS.get(account_class, account_class.title())


def _pretty_type(account_type: str | None) -> str | None:
    if not account_type:
        return None
    return _TYPE_LABELS.get(account_type, account_type.replace("_", " ").title())


def available_actual_fiscal_years(db: Session) -> list[int]:
    inv_min, inv_max = db.execute(
        select(func.min(XeroInvoice.invoice_date), func.max(XeroInvoice.invoice_date))
        .where(XeroInvoice.status.in_(list(SUMMARY_STATUSES)))
        .where(XeroInvoice.invoice_date.isnot(None))
    ).one()
    jnl_min, jnl_max = db.execute(
        select(
            func.min(XeroManualJournal.journal_date),
            func.max(XeroManualJournal.journal_date),
        )
        .where(XeroManualJournal.status.in_(list(JOURNAL_STATUSES)))
        .where(XeroManualJournal.journal_date.isnot(None))
    ).one()
    bank_min, bank_max = db.execute(
        select(
            func.min(XeroBankTransaction.transaction_date),
            func.max(XeroBankTransaction.transaction_date),
        )
        .where(XeroBankTransaction.status.in_(list(BANK_STATUSES)))
        .where(XeroBankTransaction.transaction_date.isnot(None))
    ).one()
    dates = [
        d
        for d in (inv_min, inv_max, jnl_min, jnl_max, bank_min, bank_max)
        if d is not None
    ]
    years: set[int] = set()
    if dates:
        for year in range(
            _fiscal_year_from_date(min(dates)),
            _fiscal_year_from_date(max(dates)) + 1,
        ):
            years.add(year)
    current = _fiscal_year_from_date(dt.date.today())
    years.add(current)
    return sorted(years, reverse=True)


def _invoice_section(invoice_type: str, account_class: str | None) -> str:
    if account_class in _BALANCE_CLASSES:
        return _SECTION_BALANCE
    if invoice_type == "ACCREC":
        return _SECTION_SALES
    return _SECTION_COSTS


def _journal_section_and_amount(
    account_class: str | None, line_amount: float
) -> tuple[str, float]:
    """Xero journals: debits positive, credits negative."""
    if account_class in _REVENUE_CLASSES:
        return _SECTION_SALES, -line_amount
    if account_class in _EXPENSE_CLASSES:
        return _SECTION_COSTS, line_amount
    if account_class in _BALANCE_CLASSES:
        return _SECTION_BALANCE, line_amount
    # Unknown chart mapping — treat like a cost debit for visibility.
    return _SECTION_COSTS, line_amount


def list_actuals(
    db: Session,
    *,
    fiscal_year: int,
    business: str | None = None,
) -> dict[str, Any]:
    months = _iter_month_starts(*_fiscal_year_calendar_bounds(fiscal_year))
    month_keys = [m.isoformat() for m in months]
    month_key_set = set(month_keys)
    month_labels = [
        {"month": m.isoformat(), "month_label": _month_label(m)} for m in months
    ]
    start, end = _fiscal_year_calendar_bounds(fiscal_year)

    business_value = (business or "").strip() or None
    businesses: list[str] | None = None
    if business_value in BUSINESS_GROUP_OPTIONS:
        businesses = list(BUSINESS_GROUP_OPTIONS[business_value])
    elif business_value in BUSINESS_OPTIONS:
        businesses = [business_value]
    else:
        business_value = None

    inv_stmt = (
        select(
            XeroInvoice.invoice_type,
            XeroInvoiceLine.account_code,
            XeroInvoice.invoice_date,
            XeroInvoiceLine.line_amount,
            XeroInvoice.tenant_id,
        )
        .join(XeroInvoice, XeroInvoiceLine.invoice_pk == XeroInvoice.id)
        .where(XeroInvoice.status.in_(list(SUMMARY_STATUSES)))
        .where(XeroInvoice.invoice_type.in_(("ACCREC", "ACCPAY")))
        .where(XeroInvoice.invoice_date.isnot(None))
        .where(XeroInvoice.invoice_date >= start)
        .where(XeroInvoice.invoice_date <= end)
    )
    if businesses:
        inv_stmt = inv_stmt.where(XeroInvoice.dashboard_business.in_(businesses))

    jnl_stmt = (
        select(
            XeroManualJournalLine.account_code,
            XeroManualJournal.journal_date,
            XeroManualJournalLine.line_amount,
            XeroManualJournal.tenant_id,
        )
        .join(XeroManualJournal, XeroManualJournalLine.journal_pk == XeroManualJournal.id)
        .where(XeroManualJournal.status.in_(list(JOURNAL_STATUSES)))
        .where(XeroManualJournal.journal_date.isnot(None))
        .where(XeroManualJournal.journal_date >= start)
        .where(XeroManualJournal.journal_date <= end)
    )
    if businesses:
        jnl_stmt = jnl_stmt.where(XeroManualJournal.dashboard_business.in_(businesses))

    bank_stmt = (
        select(
            XeroBankTransaction.transaction_type,
            XeroBankTransactionLine.account_code,
            XeroBankTransaction.transaction_date,
            XeroBankTransactionLine.line_amount,
            XeroBankTransaction.tenant_id,
        )
        .join(
            XeroBankTransaction,
            XeroBankTransactionLine.bank_transaction_pk == XeroBankTransaction.id,
        )
        .where(XeroBankTransaction.status.in_(list(BANK_STATUSES)))
        .where(XeroBankTransaction.transaction_date.isnot(None))
        .where(XeroBankTransaction.transaction_date >= start)
        .where(XeroBankTransaction.transaction_date <= end)
    )
    if businesses:
        bank_stmt = bank_stmt.where(
            XeroBankTransaction.dashboard_business.in_(businesses)
        )

    inv_rows = db.execute(inv_stmt).all()
    jnl_rows = db.execute(jnl_stmt).all()
    bank_rows = db.execute(bank_stmt).all()
    tenant_ids = sorted(
        {
            *(str(t) for *_, t in inv_rows if t),
            *(str(t) for *_, t in jnl_rows if t),
            *(str(t) for *_, t in bank_rows if t),
        }
    )
    accounts = account_meta_lookup(db, tenant_ids=tenant_ids or None)

    buckets: dict[str, dict[str, dict[str, float]]] = {
        _SECTION_SALES: defaultdict(lambda: defaultdict(float)),
        _SECTION_COSTS: defaultdict(lambda: defaultdict(float)),
        _SECTION_BALANCE: defaultdict(lambda: defaultdict(float)),
    }

    for invoice_type, account_code, invoice_date, line_amount, _tenant_id in inv_rows:
        if invoice_date is None:
            continue
        code = (str(account_code).strip() if account_code else "") or "Uncoded"
        month_iso = invoice_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        meta = accounts.get(code) if code != "Uncoded" else None
        account_class = meta["account_class"] if meta else None
        section = _invoice_section(invoice_type, account_class)
        buckets[section][code][month_iso] += float(line_amount or 0.0)

    for account_code, journal_date, line_amount, _tenant_id in jnl_rows:
        if journal_date is None:
            continue
        code = (str(account_code).strip() if account_code else "") or "Uncoded"
        month_iso = journal_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        meta = accounts.get(code) if code != "Uncoded" else None
        account_class = meta["account_class"] if meta else None
        section, amount = _journal_section_and_amount(
            account_class, float(line_amount or 0.0)
        )
        buckets[section][code][month_iso] += amount

    for transaction_type, account_code, transaction_date, line_amount, _tenant_id in bank_rows:
        if transaction_date is None or not is_pnl_bank_type(transaction_type):
            continue
        invoice_type = bank_type_as_invoice_type(transaction_type)
        if invoice_type is None:
            continue
        code = (str(account_code).strip() if account_code else "") or "Uncoded"
        month_iso = transaction_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        meta = accounts.get(code) if code != "Uncoded" else None
        account_class = meta["account_class"] if meta else None
        section = _invoice_section(invoice_type, account_class)
        buckets[section][code][month_iso] += float(line_amount or 0.0)

    def build_section(section_key: str, label: str, statement: str) -> dict[str, Any]:
        section_rows: list[dict[str, Any]] = []
        month_totals = {key: 0.0 for key in month_keys}
        codes = sorted(
            buckets[section_key].keys(),
            key=lambda code: (
                _class_sort_key(
                    (accounts.get(code) or {}).get("account_class")
                    if code != "Uncoded"
                    else None
                ),
                _code_sort_key(code),
            ),
        )
        for code in codes:
            amounts = buckets[section_key][code]
            month_values = []
            row_total = 0.0
            for key in month_keys:
                value = round(float(amounts.get(key, 0.0)), 2)
                month_values.append({"month": key, "amount": value})
                row_total += value
                month_totals[key] += value
            meta = accounts.get(code) if code != "Uncoded" else None
            account_class = meta["account_class"] if meta else None
            account_type = meta["account_type"] if meta else None
            category = "Uncoded" if code == "Uncoded" else (meta["name"] if meta else code)
            if abs(row_total) < 0.005:
                continue
            section_rows.append(
                {
                    "account_code": code if code != "Uncoded" else None,
                    "category": category,
                    "account_class": account_class,
                    "account_class_label": _pretty_class(account_class),
                    "account_type": account_type,
                    "account_type_label": _pretty_type(account_type),
                    "statement": statement,
                    "label": f"{code} · {category}" if code != "Uncoded" else category,
                    "months": month_values,
                    "total": round(row_total, 2),
                }
            )
        totals = [
            {"month": key, "amount": round(month_totals[key], 2)} for key in month_keys
        ]
        return {
            "key": section_key,
            "label": label,
            "statement": statement,
            "rows": section_rows,
            "totals": {
                "months": totals,
                "total": round(sum(month_totals.values()), 2),
            },
        }

    account_count = db.scalar(select(func.count()).select_from(XeroAccount)) or 0
    journal_count = (
        db.scalar(
            select(func.count())
            .select_from(XeroManualJournal)
            .where(XeroManualJournal.status.in_(list(JOURNAL_STATUSES)))
        )
        or 0
    )
    bank_count = (
        db.scalar(
            select(func.count())
            .select_from(XeroBankTransaction)
            .where(XeroBankTransaction.status.in_(list(BANK_STATUSES)))
        )
        or 0
    )

    sections = [
        build_section(_SECTION_SALES, "Sales (P&L)", "P&L"),
        build_section(_SECTION_COSTS, "Costs (P&L)", "P&L"),
        build_section(_SECTION_BALANCE, "Balance sheet", "Balance sheet"),
    ]

    return {
        "fiscal_year": fiscal_year,
        "business": business_value,
        "businesses": businesses,
        "business_options": list(BUSINESS_OPTIONS),
        "business_group_options": list(BUSINESS_GROUP_OPTIONS.keys()),
        "fiscal_year_options": available_actual_fiscal_years(db),
        "month_labels": month_labels,
        "included_statuses": sorted(SUMMARY_STATUSES),
        "journal_statuses": sorted(JOURNAL_STATUSES),
        "bank_statuses": sorted(BANK_STATUSES),
        "accounts_synced": int(account_count) > 0,
        "journals_synced": int(journal_count) > 0,
        "bank_transactions_synced": int(bank_count) > 0,
        "amount_basis": "ex_vat_invoices_journals_and_bank_transactions",
        "sections": sections,
    }
