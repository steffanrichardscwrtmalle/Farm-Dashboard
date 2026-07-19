"""Map Xero chart-of-accounts categories to financial budget headings."""

from __future__ import annotations

import datetime as dt
import re
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    FinancialForecastMapping,
    XeroAccount,
    XeroAccountBudgetMapping,
    XeroBankTransaction,
    XeroBankTransactionLine,
    XeroInvoice,
    XeroInvoiceLine,
    XeroManualJournal,
    XeroManualJournalLine,
    XeroOrganisation,
)
from app.services.financial_forecasts import (
    list_financial_mappings,
    seed_financial_forecasts_if_empty,
)
from app.services.xero_auth import XeroAuthError
from app.services.xero_bank_transactions import BANK_STATUSES, is_pnl_bank_type
from app.services.xero_invoices import SUMMARY_STATUSES
from app.services.xero_journals import JOURNAL_STATUSES

_PNL_CLASSES = frozenset({"REVENUE", "EXPENSE"})
_ACTIVE_STATUSES = frozenset({"ACTIVE", ""})
_SUGGEST_MIN_SCORE = 0.72
_ACTIVITY_MIN = 0.005
_NAME_NOISE_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _is_active_status(status: str | None) -> bool:
    return (status or "").strip().upper() in _ACTIVE_STATUSES


def _normalize_name(value: str | None) -> str:
    text = _NAME_NOISE_RE.sub(" ", (value or "").lower())
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _score_heading(
    *,
    account_name: str,
    account_class: str | None,
    heading: dict[str, Any],
) -> float:
    account_norm = _normalize_name(account_name)
    if not account_norm:
        return 0.0

    heading_norm = _normalize_name(heading.get("heading"))
    group_norm = _normalize_name(heading.get("group"))
    band_norm = _normalize_name(heading.get("band"))
    label_norm = _normalize_name(
        f"{heading.get('band')} {heading.get('group')} {heading.get('heading')}"
    )

    score = 0.0
    if account_norm == heading_norm:
        score = 1.0
    elif account_norm == group_norm:
        score = 0.96
    elif heading_norm and (heading_norm in account_norm or account_norm in heading_norm):
        score = 0.9
    elif group_norm and (group_norm in account_norm or account_norm in group_norm):
        score = 0.86
    else:
        seq = SequenceMatcher(None, account_norm, heading_norm).ratio() if heading_norm else 0.0
        tokens = max(
            _token_overlap(account_norm, heading_norm),
            _token_overlap(account_norm, group_norm),
            _token_overlap(account_norm, label_norm),
        )
        score = max(seq, tokens)

    item_type = str(heading.get("item_type") or "")
    if account_class in _PNL_CLASSES and item_type == "Profit & Loss":
        score += 0.04
    elif account_class in _PNL_CLASSES and item_type == "Cash":
        score -= 0.08
    if band_norm and band_norm in account_norm:
        score += 0.02

    return min(score, 1.0)


def suggest_heading_for_account(
    *,
    account_name: str,
    account_class: str | None,
    heading_options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for heading in heading_options:
        score = _score_heading(
            account_name=account_name,
            account_class=account_class,
            heading=heading,
        )
        if score > best_score:
            best_score = score
            best = heading
    if best is None or best_score < _SUGGEST_MIN_SCORE:
        return None
    return {
        "mapping_id": best["id"],
        "label": best["label"],
        "heading": best["heading"],
        "score": round(best_score, 3),
    }


def _heading_options(db: Session) -> list[dict[str, Any]]:
    seed_financial_forecasts_if_empty(db)
    items = list_financial_mappings(db)
    return [
        {
            "id": item["id"],
            "label": f"{item['item_type']} › {item['band']} › {item['group']} › {item['heading']}",
            "heading": item["heading"],
            "item_type": item["item_type"],
            "band": item["band"],
            "group": item["group"],
        }
        for item in items
    ]


def _activity_keys(db: Session) -> tuple[set[tuple[str, str]], set[tuple[str, str]], dict[tuple[str, str], float]]:
    """Accounts with non-zero invoice/journal activity.

    Returns:
      account_ids: {(tenant_id, account_id)}
      account_codes: {(tenant_id, account_code)}
      amounts_by_account_id: {(tenant_id, account_id): net_amount}
    """
    account_ids: set[tuple[str, str]] = set()
    account_codes: set[tuple[str, str]] = set()
    amounts_by_account_id: dict[tuple[str, str], float] = {}

    inv_id_rows = db.execute(
        select(
            XeroInvoiceLine.tenant_id,
            XeroInvoiceLine.account_id,
            func.coalesce(func.sum(XeroInvoiceLine.line_amount), 0.0),
        )
        .join(XeroInvoice, XeroInvoiceLine.invoice_pk == XeroInvoice.id)
        .where(XeroInvoice.status.in_(list(SUMMARY_STATUSES)))
        .where(XeroInvoiceLine.account_id.isnot(None))
        .where(XeroInvoiceLine.account_id != "")
        .group_by(XeroInvoiceLine.tenant_id, XeroInvoiceLine.account_id)
    ).all()
    for tenant_id, account_id, total in inv_id_rows:
        amount = float(total or 0.0)
        if abs(amount) < _ACTIVITY_MIN:
            continue
        key = (str(tenant_id), str(account_id))
        account_ids.add(key)
        amounts_by_account_id[key] = amounts_by_account_id.get(key, 0.0) + amount

    inv_code_rows = db.execute(
        select(
            XeroInvoiceLine.tenant_id,
            XeroInvoiceLine.account_code,
            func.coalesce(func.sum(XeroInvoiceLine.line_amount), 0.0),
        )
        .join(XeroInvoice, XeroInvoiceLine.invoice_pk == XeroInvoice.id)
        .where(XeroInvoice.status.in_(list(SUMMARY_STATUSES)))
        .where(XeroInvoiceLine.account_code.isnot(None))
        .where(XeroInvoiceLine.account_code != "")
        .group_by(XeroInvoiceLine.tenant_id, XeroInvoiceLine.account_code)
    ).all()
    for tenant_id, account_code, total in inv_code_rows:
        if abs(float(total or 0.0)) < _ACTIVITY_MIN:
            continue
        account_codes.add((str(tenant_id), str(account_code).strip()))

    jnl_id_rows = db.execute(
        select(
            XeroManualJournalLine.tenant_id,
            XeroManualJournalLine.account_id,
            func.coalesce(func.sum(XeroManualJournalLine.line_amount), 0.0),
        )
        .join(
            XeroManualJournal,
            XeroManualJournalLine.journal_pk == XeroManualJournal.id,
        )
        .where(XeroManualJournal.status.in_(list(JOURNAL_STATUSES)))
        .where(XeroManualJournalLine.account_id.isnot(None))
        .where(XeroManualJournalLine.account_id != "")
        .group_by(XeroManualJournalLine.tenant_id, XeroManualJournalLine.account_id)
    ).all()
    for tenant_id, account_id, total in jnl_id_rows:
        amount = float(total or 0.0)
        if abs(amount) < _ACTIVITY_MIN:
            continue
        key = (str(tenant_id), str(account_id))
        account_ids.add(key)
        amounts_by_account_id[key] = amounts_by_account_id.get(key, 0.0) + amount

    jnl_code_rows = db.execute(
        select(
            XeroManualJournalLine.tenant_id,
            XeroManualJournalLine.account_code,
            func.coalesce(func.sum(XeroManualJournalLine.line_amount), 0.0),
        )
        .join(
            XeroManualJournal,
            XeroManualJournalLine.journal_pk == XeroManualJournal.id,
        )
        .where(XeroManualJournal.status.in_(list(JOURNAL_STATUSES)))
        .where(XeroManualJournalLine.account_code.isnot(None))
        .where(XeroManualJournalLine.account_code != "")
        .group_by(XeroManualJournalLine.tenant_id, XeroManualJournalLine.account_code)
    ).all()
    for tenant_id, account_code, total in jnl_code_rows:
        if abs(float(total or 0.0)) < _ACTIVITY_MIN:
            continue
        account_codes.add((str(tenant_id), str(account_code).strip()))

    bank_id_rows = db.execute(
        select(
            XeroBankTransactionLine.tenant_id,
            XeroBankTransactionLine.account_id,
            XeroBankTransaction.transaction_type,
            func.coalesce(func.sum(XeroBankTransactionLine.line_amount), 0.0),
        )
        .join(
            XeroBankTransaction,
            XeroBankTransactionLine.bank_transaction_pk == XeroBankTransaction.id,
        )
        .where(XeroBankTransaction.status.in_(list(BANK_STATUSES)))
        .where(XeroBankTransactionLine.account_id.isnot(None))
        .where(XeroBankTransactionLine.account_id != "")
        .group_by(
            XeroBankTransactionLine.tenant_id,
            XeroBankTransactionLine.account_id,
            XeroBankTransaction.transaction_type,
        )
    ).all()
    for tenant_id, account_id, transaction_type, total in bank_id_rows:
        if not is_pnl_bank_type(transaction_type):
            continue
        amount = float(total or 0.0)
        if abs(amount) < _ACTIVITY_MIN:
            continue
        key = (str(tenant_id), str(account_id))
        account_ids.add(key)
        amounts_by_account_id[key] = amounts_by_account_id.get(key, 0.0) + amount

    bank_code_rows = db.execute(
        select(
            XeroBankTransactionLine.tenant_id,
            XeroBankTransactionLine.account_code,
            XeroBankTransaction.transaction_type,
            func.coalesce(func.sum(XeroBankTransactionLine.line_amount), 0.0),
        )
        .join(
            XeroBankTransaction,
            XeroBankTransactionLine.bank_transaction_pk == XeroBankTransaction.id,
        )
        .where(XeroBankTransaction.status.in_(list(BANK_STATUSES)))
        .where(XeroBankTransactionLine.account_code.isnot(None))
        .where(XeroBankTransactionLine.account_code != "")
        .group_by(
            XeroBankTransactionLine.tenant_id,
            XeroBankTransactionLine.account_code,
            XeroBankTransaction.transaction_type,
        )
    ).all()
    for tenant_id, account_code, transaction_type, total in bank_code_rows:
        if not is_pnl_bank_type(transaction_type):
            continue
        if abs(float(total or 0.0)) < _ACTIVITY_MIN:
            continue
        account_codes.add((str(tenant_id), str(account_code).strip()))

    return account_ids, account_codes, amounts_by_account_id


def _account_has_activity(
    account: XeroAccount,
    *,
    account_ids: set[tuple[str, str]],
    account_codes: set[tuple[str, str]],
) -> bool:
    if (account.tenant_id, account.account_id) in account_ids:
        return True
    code = (account.code or "").strip()
    return bool(code) and (account.tenant_id, code) in account_codes


def list_account_budget_mappings(db: Session) -> dict[str, Any]:
    organisations = {
        row.tenant_id: row
        for row in db.scalars(select(XeroOrganisation)).all()
    }
    mapping_rows = {
        (row.tenant_id, row.account_id): row
        for row in db.scalars(select(XeroAccountBudgetMapping)).all()
    }
    heading_options = _heading_options(db)
    forecast_by_id = {item["id"]: item for item in heading_options}
    active_ids, active_codes, amounts_by_id = _activity_keys(db)

    accounts = db.scalars(
        select(XeroAccount).order_by(
            XeroAccount.tenant_id.asc(),
            XeroAccount.code.asc(),
            XeroAccount.name.asc(),
        )
    ).all()

    items: list[dict[str, Any]] = []
    for account in accounts:
        if not _account_has_activity(
            account, account_ids=active_ids, account_codes=active_codes
        ):
            continue
        org = organisations.get(account.tenant_id)
        mapped = mapping_rows.get((account.tenant_id, account.account_id))
        mapping_id = mapped.mapping_id if mapped else None
        heading = forecast_by_id.get(mapping_id) if mapping_id else None
        account_class = (account.account_class or "").strip().upper() or None
        is_active = _is_active_status(account.status)
        is_mapped = mapping_id is not None
        is_pnl = account_class in _PNL_CLASSES
        suggestion = None
        if not is_mapped:
            suggestion = suggest_heading_for_account(
                account_name=account.name,
                account_class=account_class,
                heading_options=heading_options,
            )
        activity_total = amounts_by_id.get((account.tenant_id, account.account_id))
        items.append(
            {
                "tenant_id": account.tenant_id,
                "account_id": account.account_id,
                "account_code": account.code,
                "account_name": account.name,
                "account_class": account_class,
                "account_type": account.account_type,
                "status": account.status,
                "is_active": is_active,
                "dashboard_business": org.dashboard_business if org else None,
                "tenant_name": org.tenant_name if org else account.tenant_id,
                "mapping_id": mapping_id,
                "mapping_label": heading["label"] if heading else None,
                "heading": heading["heading"] if heading else None,
                "is_mapped": is_mapped,
                "is_pnl": is_pnl,
                "suggestion": suggestion,
                "activity_total": round(activity_total, 2) if activity_total is not None else None,
                "has_activity": True,
            }
        )

    # Active unmapped P&L first, then other unmapped, then mapped / archived.
    items.sort(
        key=lambda row: (
            0
            if (not row["is_mapped"] and row["is_pnl"] and row["is_active"])
            else 1
            if (not row["is_mapped"] and row["is_active"])
            else 2
            if not row["is_mapped"]
            else 3,
            row.get("dashboard_business") or "",
            row.get("account_code") or "",
            row.get("account_name") or "",
        )
    )

    return {
        "items": items,
        "heading_options": heading_options,
        "counts": mapping_summary(db),
    }


def set_account_budget_mapping(
    db: Session,
    *,
    tenant_id: str,
    account_id: str,
    mapping_id: int | None,
) -> dict[str, Any]:
    account = db.scalar(
        select(XeroAccount)
        .where(XeroAccount.tenant_id == tenant_id)
        .where(XeroAccount.account_id == account_id)
    )
    if account is None:
        raise XeroAuthError("Unknown Xero account. Sync accounts first.")

    existing = db.scalar(
        select(XeroAccountBudgetMapping)
        .where(XeroAccountBudgetMapping.tenant_id == tenant_id)
        .where(XeroAccountBudgetMapping.account_id == account_id)
    )

    if mapping_id is None:
        if existing is not None:
            db.delete(existing)
            db.commit()
        return {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "account_code": account.code,
            "mapping_id": None,
            "is_mapped": False,
        }

    heading = db.get(FinancialForecastMapping, mapping_id)
    if heading is None:
        raise XeroAuthError("Unknown budget heading.")

    if existing is None:
        existing = XeroAccountBudgetMapping(
            tenant_id=tenant_id,
            account_id=account_id,
            account_code=account.code,
            mapping_id=mapping_id,
        )
        db.add(existing)
    else:
        existing.mapping_id = mapping_id
        existing.account_code = account.code
        existing.updated_at = _utcnow()
    db.commit()

    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "account_code": account.code,
        "mapping_id": mapping_id,
        "heading": heading.heading,
        "is_mapped": True,
    }


def mapping_summary(db: Session) -> dict[str, Any]:
    active_ids, active_codes, _amounts = _activity_keys(db)
    accounts = [
        account
        for account in db.scalars(select(XeroAccount)).all()
        if _account_has_activity(
            account, account_ids=active_ids, account_codes=active_codes
        )
    ]
    mapped_keys = {
        (row.tenant_id, row.account_id)
        for row in db.scalars(select(XeroAccountBudgetMapping)).all()
    }
    # Only categories with financial activity count toward mapping progress.
    relevant = [a for a in accounts if _is_active_status(a.status)]
    relevant_pnl = [
        a
        for a in relevant
        if (a.account_class or "").strip().upper() in _PNL_CLASSES
    ]
    mapped = sum(1 for a in relevant if (a.tenant_id, a.account_id) in mapped_keys)
    pnl_mapped = sum(
        1 for a in relevant_pnl if (a.tenant_id, a.account_id) in mapped_keys
    )
    return {
        "accounts": len(relevant),
        "mapped": mapped,
        "unmapped": max(len(relevant) - mapped, 0),
        "pnl_accounts": len(relevant_pnl),
        "pnl_mapped": pnl_mapped,
        "unmapped_pnl": max(len(relevant_pnl) - pnl_mapped, 0),
    }


def clear_account_budget_mappings(db: Session) -> None:
    db.execute(delete(XeroAccountBudgetMapping))
    db.commit()
