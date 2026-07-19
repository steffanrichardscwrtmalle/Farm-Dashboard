"""Xero P&L actuals grid aligned to financial budget headings."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BUSINESS_GROUP_OPTIONS,
    BUSINESS_OPTIONS,
    FinancialForecastLine,
    FinancialForecastMapping,
    MilkStatement,
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
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
    _month_start,
)
from app.services.financial_forecasts import (
    list_band_definitions,
    seed_financial_forecasts_if_empty,
)
from app.services.xero_actuals import available_actual_fiscal_years
from app.services.xero_bank_transactions import (
    BANK_STATUSES,
    bank_type_as_invoice_type,
    is_pnl_bank_type,
)
from app.services.xero_invoices import SUMMARY_STATUSES
from app.services.xero_journals import JOURNAL_STATUSES

_REVENUE_CLASSES = frozenset({"REVENUE"})
_EXPENSE_CLASSES = frozenset({"EXPENSE"})
_PNL_ITEM_TYPE = "Profit & Loss"

# Dashboard business → milk statement farms (litres are not reliable in Xero).
_BUSINESS_MILK_FARMS: dict[str, tuple[str, ...]] = {
    "Cwrt Malle": ("CM",),
    "Green Acre Dairy": ("GAD",),
    "H&S Forage": (),
    "Cwrt Malle + H&S Forage": ("CM",),
}


def _month_label(value: dt.date) -> str:
    return value.strftime("%b-%y")


def _last_day_of_month(value: dt.date) -> dt.date:
    if value.month == 12:
        return dt.date(value.year, 12, 31)
    return dt.date(value.year, value.month + 1, 1) - dt.timedelta(days=1)


def _latest_selectable_month(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    return (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)


def available_pnl_month_options(db: Session) -> list[dict[str, str]]:
    """Month options for the Range slider (earliest FY Apr through last complete month)."""
    years = available_actual_fiscal_years(db)
    if not years:
        return []
    start, _ = _fiscal_year_calendar_bounds(min(years))
    _, fy_end = _fiscal_year_calendar_bounds(max(years))
    end = min(_month_start(fy_end), _month_start(_latest_selectable_month()))
    if end < start:
        end = start
    return [
        {"month": m.isoformat(), "month_label": _month_label(m)}
        for m in _iter_month_starts(start, end)
    ]


def _milk_litres_by_month(
    db: Session,
    *,
    farms: list[str],
    month_keys: list[str],
) -> dict[str, float | None]:
    out: dict[str, float | None] = {key: None for key in month_keys}
    if not farms or not month_keys:
        return out
    start = dt.date.fromisoformat(month_keys[0])
    end = dt.date.fromisoformat(month_keys[-1])
    rows = db.scalars(
        select(MilkStatement).where(
            MilkStatement.farm.in_(farms),
            MilkStatement.statement_month >= start,
            MilkStatement.statement_month <= end,
        )
    ).all()
    totals: dict[str, float] = defaultdict(float)
    seen: set[str] = set()
    for row in rows:
        if row.statement_month is None:
            continue
        key = row.statement_month.isoformat()
        if key not in out:
            continue
        seen.add(key)
        totals[key] += float(row.litres_sold or 0.0)
    for key in seen:
        out[key] = totals[key]
    return out


def _resolve_businesses(business: str | None) -> tuple[str | None, list[str] | None]:
    business_value = (business or "").strip() or None
    if business_value in BUSINESS_GROUP_OPTIONS:
        return business_value, list(BUSINESS_GROUP_OPTIONS[business_value])
    if business_value in BUSINESS_OPTIONS:
        return business_value, [business_value]
    return None, None


def _mapped_categories_by_heading(
    db: Session,
    *,
    businesses: list[str] | None,
) -> dict[int, list[dict[str, Any]]]:
    """Xero account categories mapped to each budget heading, scoped to selected businesses."""
    organisations = {
        row.tenant_id: row for row in db.scalars(select(XeroOrganisation)).all()
    }
    if businesses:
        allowed_tenants = {
            tenant_id
            for tenant_id, org in organisations.items()
            if org.dashboard_business in businesses
        }
    else:
        allowed_tenants = None

    mapping_rows = db.scalars(select(XeroAccountBudgetMapping)).all()
    account_keys = {(row.tenant_id, row.account_id) for row in mapping_rows}
    accounts = {
        (row.tenant_id, row.account_id): row
        for row in db.scalars(select(XeroAccount)).all()
        if (row.tenant_id, row.account_id) in account_keys
    }

    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for mapped in mapping_rows:
        if allowed_tenants is not None and mapped.tenant_id not in allowed_tenants:
            continue
        account = accounts.get((mapped.tenant_id, mapped.account_id))
        org = organisations.get(mapped.tenant_id)
        code = (account.code if account else mapped.account_code) or ""
        name = (account.name if account else "") or ""
        out[int(mapped.mapping_id)].append(
            {
                "account_code": code,
                "account_name": name,
                "dashboard_business": org.dashboard_business if org else None,
            }
        )

    for mapping_id, items in out.items():
        items.sort(
            key=lambda item: (
                item.get("dashboard_business") or "",
                item.get("account_code") or "",
                item.get("account_name") or "",
            )
        )
        out[mapping_id] = items
    return dict(out)


def _milk_farms_for_business(business_value: str | None) -> list[str]:
    if business_value is None:
        return ["CM", "GAD"]
    return list(_BUSINESS_MILK_FARMS.get(business_value, ()))


def _milk_farms_for_completed_default(business_value: str | None) -> list[str]:
    """CM/GAD farms used to default Completed through (never H&S Forage)."""
    farms = _milk_farms_for_business(business_value)
    return farms if farms else ["CM", "GAD"]


def _latest_milk_litres_month(
    db: Session,
    *,
    farms: list[str],
) -> dt.date | None:
    """Most recent statement month with litres_sold for the given farms.

    When more than one farm is supplied (e.g. All businesses → CM + GAD), the
    month must have litres_sold for every farm — not just the latest of either.
    """
    if not farms:
        return None
    rows = db.execute(
        select(MilkStatement.statement_month, MilkStatement.farm).where(
            MilkStatement.farm.in_(farms),
            MilkStatement.litres_sold.isnot(None),
        )
    ).all()
    if not rows:
        return None

    farm_set = set(farms)
    by_month: dict[dt.date, set[str]] = defaultdict(set)
    for statement_month, farm in rows:
        if statement_month is None or not farm:
            continue
        by_month[_month_start(statement_month)].add(str(farm).strip().upper())

    if len(farm_set) == 1:
        candidates = list(by_month.keys())
    else:
        candidates = [
            month
            for month, present in by_month.items()
            if farm_set <= present
        ]
    if not candidates:
        return None
    return max(candidates)


def _budget_farms_for_business(business_value: str | None) -> list[str]:
    """Financial forecast farms (CM/GAD only) for the selected P&L business."""
    return _milk_farms_for_business(business_value)


def _budget_amounts_by_mapping(
    db: Session,
    *,
    farms: list[str],
    months: list[dt.date],
    mapping_ids: list[int] | None = None,
) -> dict[int, dict[str, float]]:
    """Sum forecast line amounts by mapping_id → month ISO for selected farms."""
    out: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    if not farms or not months:
        return out
    stmt = select(FinancialForecastLine).where(
        FinancialForecastLine.forecast_month.in_(months),
        FinancialForecastLine.farm.in_(farms),
    )
    if mapping_ids is not None:
        if not mapping_ids:
            return out
        stmt = stmt.where(FinancialForecastLine.mapping_id.in_(mapping_ids))
    for line in db.scalars(stmt).all():
        if line.amount is None:
            continue
        key = line.forecast_month.isoformat()
        out[int(line.mapping_id)][key] += float(line.amount)
    return out


def _budget_litre_mapping_ids(db: Session) -> list[int]:
    return [
        int(row.id)
        for row in db.scalars(
            select(FinancialForecastMapping).where(
                FinancialForecastMapping.item_type == "Data"
            )
        ).all()
        if "litre" in (row.heading or "").lower()
    ]


def _signed_amount(
    *,
    account_class: str | None,
    invoice_type: str | None,
    line_amount: float,
    is_journal: bool,
) -> float:
    amount = float(line_amount or 0.0)
    if is_journal:
        # Xero journals: debits positive, credits negative.
        if account_class in _REVENUE_CLASSES:
            return -amount
        if account_class in _EXPENSE_CLASSES:
            return amount
        return amount
    if account_class in _REVENUE_CLASSES:
        return amount if invoice_type == "ACCREC" else -amount
    if account_class in _EXPENSE_CLASSES:
        return amount if invoice_type == "ACCPAY" else -amount
    return amount if invoice_type == "ACCREC" else amount


def _lookup_maps(db: Session) -> tuple[
    dict[tuple[str, str], int],
    dict[tuple[str, str], str | None],
    dict[tuple[str, str], str],
]:
    mapping_by_account_id = {
        (row.tenant_id, row.account_id): row.mapping_id
        for row in db.scalars(select(XeroAccountBudgetMapping)).all()
    }
    account_class_by_id: dict[tuple[str, str], str | None] = {}
    account_id_by_code: dict[tuple[str, str], str] = {}
    for account in db.scalars(select(XeroAccount)).all():
        key = (account.tenant_id, account.account_id)
        account_class_by_id[key] = (
            str(account.account_class).strip().upper() if account.account_class else None
        )
        code = (account.code or "").strip()
        if code:
            account_id_by_code[(account.tenant_id, code)] = account.account_id
    return mapping_by_account_id, account_class_by_id, account_id_by_code


def _resolve_mapping(
    *,
    tenant_id: str,
    account_id: str | None,
    account_code: str | None,
    mapping_by_account_id: dict[tuple[str, str], int],
    account_id_by_code: dict[tuple[str, str], str],
    account_class_by_id: dict[tuple[str, str], str | None],
) -> tuple[int | None, str | None]:
    aid = (account_id or "").strip() or None
    if not aid:
        code = (account_code or "").strip()
        if code:
            aid = account_id_by_code.get((tenant_id, code))
    if not aid:
        return None, None
    mapping_id = mapping_by_account_id.get((tenant_id, aid))
    account_class = account_class_by_id.get((tenant_id, aid))
    return mapping_id, account_class


def list_xero_pnl(
    db: Session,
    *,
    fiscal_year: int | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    business: str | None = None,
) -> dict[str, Any]:
    seed_financial_forecasts_if_empty(db)

    if month_from is not None and month_to is not None:
        start_month = _month_start(month_from)
        end_month = _month_start(month_to)
        if end_month < start_month:
            start_month, end_month = end_month, start_month
        months = _iter_month_starts(start_month, end_month)
        mode = "range"
        fiscal_year = _fiscal_year_from_date(end_month)
    elif fiscal_year is not None:
        months = _iter_month_starts(*_fiscal_year_calendar_bounds(fiscal_year))
        mode = "fiscal_year"
    else:
        raise ValueError("Provide fiscal_year or month_from/month_to.")

    if not months:
        raise ValueError("Month range is empty.")

    month_keys = [m.isoformat() for m in months]
    month_key_set = set(month_keys)
    start, end = months[0], _last_day_of_month(months[-1])
    business_value, businesses = _resolve_businesses(business)

    mapping_by_account_id, account_class_by_id, account_id_by_code = _lookup_maps(db)

    # mapping_id -> month -> amount
    buckets: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unmapped = {key: 0.0 for key in month_keys}

    inv_stmt = (
        select(
            XeroInvoice.invoice_type,
            XeroInvoiceLine.account_id,
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

    for invoice_type, account_id, account_code, invoice_date, line_amount, tenant_id in db.execute(
        inv_stmt
    ):
        if invoice_date is None:
            continue
        month_iso = invoice_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        mapping_id, account_class = _resolve_mapping(
            tenant_id=str(tenant_id),
            account_id=account_id,
            account_code=account_code,
            mapping_by_account_id=mapping_by_account_id,
            account_id_by_code=account_id_by_code,
            account_class_by_id=account_class_by_id,
        )
        signed = _signed_amount(
            account_class=account_class,
            invoice_type=invoice_type,
            line_amount=float(line_amount or 0.0),
            is_journal=False,
        )
        if mapping_id is None:
            unmapped[month_iso] += signed
        else:
            buckets[mapping_id][month_iso] += signed

    jnl_stmt = (
        select(
            XeroManualJournalLine.account_id,
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

    for account_id, account_code, journal_date, line_amount, tenant_id in db.execute(jnl_stmt):
        if journal_date is None:
            continue
        month_iso = journal_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        mapping_id, account_class = _resolve_mapping(
            tenant_id=str(tenant_id),
            account_id=account_id,
            account_code=account_code,
            mapping_by_account_id=mapping_by_account_id,
            account_id_by_code=account_id_by_code,
            account_class_by_id=account_class_by_id,
        )
        signed = _signed_amount(
            account_class=account_class,
            invoice_type=None,
            line_amount=float(line_amount or 0.0),
            is_journal=True,
        )
        if mapping_id is None:
            unmapped[month_iso] += signed
        else:
            buckets[mapping_id][month_iso] += signed

    bank_stmt = (
        select(
            XeroBankTransaction.transaction_type,
            XeroBankTransactionLine.account_id,
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

    for (
        transaction_type,
        account_id,
        account_code,
        transaction_date,
        line_amount,
        tenant_id,
    ) in db.execute(bank_stmt):
        if transaction_date is None or not is_pnl_bank_type(transaction_type):
            continue
        invoice_type = bank_type_as_invoice_type(transaction_type)
        month_iso = transaction_date.replace(day=1).isoformat()
        if month_iso not in month_key_set:
            continue
        mapping_id, account_class = _resolve_mapping(
            tenant_id=str(tenant_id),
            account_id=account_id,
            account_code=account_code,
            mapping_by_account_id=mapping_by_account_id,
            account_id_by_code=account_id_by_code,
            account_class_by_id=account_class_by_id,
        )
        signed = _signed_amount(
            account_class=account_class,
            invoice_type=invoice_type,
            line_amount=float(line_amount or 0.0),
            is_journal=False,
        )
        if mapping_id is None:
            unmapped[month_iso] += signed
        else:
            buckets[mapping_id][month_iso] += signed

    # Milk litres from statements (not Xero).
    milk_farms = _milk_farms_for_business(business_value)
    milk_months = _milk_litres_by_month(db, farms=milk_farms, month_keys=month_keys)
    completed_default_farms = _milk_farms_for_completed_default(business_value)
    default_completed_month = _latest_milk_litres_month(
        db, farms=completed_default_farms
    )

    # Budget figures from Financial Forecasts (CM/GAD).
    budget_farms = _budget_farms_for_business(business_value)
    bands = [
        band
        for band in list_band_definitions(db)
        if band.get("item_type") == _PNL_ITEM_TYPE
    ]
    pnl_mapping_ids = [
        int(heading_info["mapping_id"])
        for band_def in bands
        for heading_info in band_def["headings"]
    ]
    litre_mapping_ids = _budget_litre_mapping_ids(db)
    budget_by_mapping = _budget_amounts_by_mapping(
        db,
        farms=budget_farms,
        months=months,
        mapping_ids=sorted(set(pnl_mapping_ids + litre_mapping_ids)),
    )
    budget_litres_by_month: dict[str, float] = {key: 0.0 for key in month_keys}
    for mapping_id in litre_mapping_ids:
        for key, value in budget_by_mapping.get(mapping_id, {}).items():
            if key in budget_litres_by_month:
                budget_litres_by_month[key] += value

    milk_row = {
        "mapping_id": None,
        "key": "milk_litres_sold",
        "item_type": "Data",
        "band": "Milk",
        "group": "Milk",
        "heading": "Milk Litres Sold",
        "unit": "litres",
        "source": "milk_statements.litres_sold",
        "milk_farms": milk_farms,
        "months": [
            {
                "month": key,
                "month_label": _month_label(dt.date.fromisoformat(key)),
                "amount": milk_months[key],
                "budget": (
                    round(budget_litres_by_month[key], 2)
                    if budget_litres_by_month[key]
                    else None
                ),
            }
            for key in month_keys
        ],
    }

    categories_by_heading = _mapped_categories_by_heading(db, businesses=businesses)

    grid_rows: list[dict[str, Any]] = [milk_row]
    for band_def in bands:
        invert_valuation = band_def.get("band") == "Valuation Change"
        for heading_info in band_def["headings"]:
            mapping_id = int(heading_info["mapping_id"])
            amounts = buckets.get(mapping_id, {})
            budgets = budget_by_mapping.get(mapping_id, {})
            month_rows = []
            row_total = 0.0
            budget_total = 0.0
            for key in month_keys:
                value = float(amounts.get(key, 0.0))
                budget_value = float(budgets.get(key, 0.0))
                # Xero actuals: invert Valuation Change on load (budget is inverted on autofill import).
                if invert_valuation:
                    value *= -1
                value = round(value, 2)
                budget_value = round(budget_value, 2)
                month_rows.append(
                    {
                        "month": key,
                        "month_label": _month_label(dt.date.fromisoformat(key)),
                        "amount": value,
                        "budget": budget_value,
                    }
                )
                row_total += value
                budget_total += budget_value
            grid_rows.append(
                {
                    "mapping_id": mapping_id,
                    "item_type": band_def["item_type"],
                    "band": band_def["band"],
                    "group": heading_info["group"],
                    "heading": heading_info["heading"],
                    "unit": "gbp",
                    "mapped_categories": categories_by_heading.get(mapping_id, []),
                    "months": month_rows,
                    "total": round(row_total, 2),
                    "budget_total": round(budget_total, 2),
                }
            )

    unmapped_months = [
        {
            "month": key,
            "month_label": _month_label(dt.date.fromisoformat(key)),
            "amount": round(unmapped[key], 2),
        }
        for key in month_keys
    ]

    return {
        "mode": mode,
        "fiscal_year": fiscal_year,
        "month_from": months[0].isoformat(),
        "month_to": months[-1].isoformat(),
        "business": business_value,
        "businesses": businesses,
        "business_options": list(BUSINESS_OPTIONS),
        "business_group_options": list(BUSINESS_GROUP_OPTIONS.keys()),
        "fiscal_year_options": available_actual_fiscal_years(db),
        "range_month_options": available_pnl_month_options(db),
        "month_labels": [
            {"month": m.isoformat(), "month_label": _month_label(m)} for m in months
        ],
        "grid_rows": grid_rows,
        "unmapped": {
            "months": unmapped_months,
            "total": round(sum(unmapped.values()), 2),
        },
        "milk_farms": milk_farms,
        "budget_farms": budget_farms,
        "default_completed_month": (
            default_completed_month.isoformat() if default_completed_month else None
        ),
        "default_completed_month_farms": completed_default_farms,
    }
