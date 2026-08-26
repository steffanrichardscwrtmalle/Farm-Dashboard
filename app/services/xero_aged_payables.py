"""Aged payables: outstanding supplier bills summarised by contact and invoice month."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BUSINESS_GROUP_OPTIONS, BUSINESS_OPTIONS, XeroInvoice
from app.services.xero_invoices import CREDIT_NOTE_TYPES

AGED_PAYABLE_TYPES = frozenset({"ACCPAY", "ACCPAYCREDIT"})
AGED_PAYABLE_STATUSES = frozenset({"AUTHORISED"})
OLDER_COLUMN_KEY = "older"
RECENT_MONTH_COUNT = 4
_AMOUNT_EPS = 0.00499

# Short labels used on the Aged Payables page (GAD / CM / H&S / CM + H&S).
AGED_PAYABLE_VIEWS: tuple[dict[str, Any], ...] = (
    {
        "id": "GAD",
        "label": "GAD",
        "businesses": ("Green Acre Dairy",),
    },
    {
        "id": "CM",
        "label": "CM",
        "businesses": ("Cwrt Malle",),
    },
    {
        "id": "H&S",
        "label": "H&S",
        "businesses": ("H&S Forage",),
    },
    {
        "id": "CM + H&S",
        "label": "CM + H&S",
        "businesses": ("Cwrt Malle", "H&S Forage"),
    },
)
DEFAULT_AGED_PAYABLE_VIEW = "GAD"

_VIEW_BY_ID = {str(view["id"]): view for view in AGED_PAYABLE_VIEWS}
_ALIAS_TO_VIEW_ID: dict[str, str] = {}
for _view in AGED_PAYABLE_VIEWS:
    _view_id = str(_view["id"])
    _ALIAS_TO_VIEW_ID[_view_id.casefold()] = _view_id
    _ALIAS_TO_VIEW_ID[str(_view["label"]).casefold()] = _view_id
    businesses = tuple(_view["businesses"])
    if len(businesses) == 1:
        _ALIAS_TO_VIEW_ID[businesses[0].casefold()] = _view_id
    else:
        group_label = next(
            (
                name
                for name, members in BUSINESS_GROUP_OPTIONS.items()
                if tuple(members) == businesses
            ),
            None,
        )
        if group_label:
            _ALIAS_TO_VIEW_ID[group_label.casefold()] = _view_id


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _signed_amount_due(invoice_type: str | None, amount_due: float | None) -> float:
    remaining = float(amount_due or 0.0)
    if (invoice_type or "") in CREDIT_NOTE_TYPES:
        return -abs(remaining)
    return remaining


def resolve_aged_payable_view(business: str | None) -> dict[str, Any]:
    raw = (business or "").strip() or DEFAULT_AGED_PAYABLE_VIEW
    view_id = _ALIAS_TO_VIEW_ID.get(raw.casefold())
    if view_id is None:
        allowed = ", ".join(view["label"] for view in AGED_PAYABLE_VIEWS)
        raise ValueError(f"Unknown aged payables view {raw!r}. Use one of: {allowed}.")
    view = _VIEW_BY_ID[view_id]
    return {
        "id": str(view["id"]),
        "label": str(view["label"]),
        "businesses": list(view["businesses"]),
    }


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _add_months(value: dt.date, delta: int) -> dt.date:
    month_index = value.year * 12 + (value.month - 1) + delta
    year, month0 = divmod(month_index, 12)
    return dt.date(year, month0 + 1, 1)


def _month_label(value: dt.date) -> str:
    return value.strftime("%b-%y")


def _recent_months(as_of: dt.date) -> list[dt.date]:
    current = _month_start(as_of)
    start_offset = 1 - RECENT_MONTH_COUNT
    return [_add_months(current, offset) for offset in range(start_offset, 1)]


def _column_key(invoice_month: dt.date, *, recent: list[dt.date]) -> str:
    if invoice_month < recent[0]:
        return OLDER_COLUMN_KEY
    if invoice_month > recent[-1]:
        return recent[-1].isoformat()
    return invoice_month.isoformat()


def list_aged_payables(
    db: Session,
    *,
    business: str | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    view = resolve_aged_payable_view(business)
    businesses = list(view["businesses"])
    as_of_date = as_of or dt.date.today()
    recent = _recent_months(as_of_date)
    display_months = list(reversed(recent))
    month_keys = [*[value.isoformat() for value in display_months], OLDER_COLUMN_KEY]
    month_labels = [*[_month_label(value) for value in display_months], "Older"]

    stmt = (
        select(
            XeroInvoice.contact_name,
            XeroInvoice.invoice_type,
            XeroInvoice.invoice_date,
            XeroInvoice.amount_due,
        )
        .where(XeroInvoice.invoice_type.in_(list(AGED_PAYABLE_TYPES)))
        .where(XeroInvoice.status.in_(list(AGED_PAYABLE_STATUSES)))
        .where(XeroInvoice.invoice_date.isnot(None))
        .where(XeroInvoice.amount_due.isnot(None))
        .where(XeroInvoice.dashboard_business.in_(businesses))
    )

    cells: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    invoice_count = 0
    for contact_name, invoice_type, invoice_date, amount_due in db.execute(stmt):
        signed = _signed_amount_due(invoice_type, amount_due)
        if abs(signed) <= _AMOUNT_EPS:
            continue
        if invoice_date is None:
            continue
        contact = (contact_name or "").strip() or "(No contact)"
        key = _column_key(_month_start(invoice_date), recent=recent)
        cells[contact][key] += signed
        invoice_count += 1

    contacts: list[dict[str, Any]] = []
    column_totals = {key: 0.0 for key in month_keys}
    grand_total = 0.0
    for contact in sorted(cells, key=lambda name: name.casefold()):
        amounts = {
            key: _round_money(value)
            for key, value in cells[contact].items()
            if abs(value) > _AMOUNT_EPS
        }
        total = _round_money(sum(amounts.values()))
        if abs(total) <= _AMOUNT_EPS and not amounts:
            continue
        contacts.append(
            {
                "contact": contact,
                "amounts": amounts,
                "total": total,
            }
        )
        for key, value in amounts.items():
            if key in column_totals:
                column_totals[key] += value
        grand_total += total

    column_totals = {key: _round_money(value) for key, value in column_totals.items()}
    last_synced = db.scalar(
        select(func.max(XeroInvoice.synced_at)).where(
            XeroInvoice.dashboard_business.in_(businesses)
        )
    )

    return {
        "business": view["id"],
        "business_label": view["label"],
        "businesses": businesses,
        "views": [
            {"id": item["id"], "label": item["label"]} for item in AGED_PAYABLE_VIEWS
        ],
        "months": month_keys,
        "month_labels": month_labels,
        "contacts": contacts,
        "column_totals": column_totals,
        "grand_total": _round_money(grand_total),
        "invoice_count": invoice_count,
        "contact_count": len(contacts),
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "business_options": list(BUSINESS_OPTIONS),
    }
