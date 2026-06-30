"""Query confirmed milk sales statements for the dashboard.

Presented as a fiscal-year grid (Apr–Mar) for one or more farms: one row per
month with the buyer's confirmed figures, plus a year total / litres-weighted
average. When more than one farm is selected each month cell is the litres-
weighted average across the selected farms.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MilkStatement
from app.services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
)

_FARM_LABELS = {"CM": "Cwrt Malle", "GAD": "Green Acre Dairy"}
_FARM_ORDER = ("CM", "GAD")
_DEFAULT_FARMS = ("CM",)

_QUALITY_FIELDS = (
    "milk_price_ppl",
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "thermoduric",
    "fpd",
)
_INT_FIELDS = ("scc", "bactoscan", "thermoduric", "fpd")


def _combine(records: Sequence[MilkStatement]) -> dict[str, Any]:
    """Litres-weighted combination of one or more statement records.

    For a single record this returns its own values (rounded); for several it
    sums litres and weights each quality figure by litres, ignoring fields a
    farm doesn't report (e.g. GAD has no thermoduric, CM has no FPD).
    """
    total_litres = sum(r.litres_sold or 0 for r in records)
    out: dict[str, Any] = {"litres_sold": total_litres or None}

    for field in _QUALITY_FIELDS:
        parts = [
            (getattr(r, field), r.litres_sold or 0)
            for r in records
            if getattr(r, field) is not None and (r.litres_sold or 0) > 0
        ]
        if not parts or total_litres <= 0:
            out[field] = None
            continue
        litres = sum(lit for _, lit in parts)
        weighted = sum(val * lit for val, lit in parts) / litres
        if field in _INT_FIELDS:
            out[field] = int(round(weighted))
        else:
            out[field] = round(weighted, 3 if field == "milk_price_ppl" else 2)

    return out


def _month_row(records: Sequence[MilkStatement], month_start: dt.date) -> dict[str, Any]:
    row: dict[str, Any] = {
        "month_label": month_start.strftime("%b-%y"),
        "statement_month": month_start.isoformat(),
        "has_data": bool(records),
    }
    row.update(_combine(records))
    return row


def _normalise_farms(farms: Sequence[str] | None) -> list[str]:
    if not farms:
        return list(_DEFAULT_FARMS)
    selected = {f.strip().upper() for f in farms if f and f.strip()}
    ordered = [f for f in _FARM_ORDER if f in selected]
    return ordered or list(_DEFAULT_FARMS)


def list_milk_statements(
    db: Session,
    *,
    fiscal_year: int | None = None,
    farms: Sequence[str] | None = None,
) -> dict[str, Any]:
    all_rows = db.scalars(select(MilkStatement)).all()

    fiscal_year_options = sorted(
        {
            _fiscal_year_from_date(r.statement_month)
            for r in all_rows
            if r.statement_month
        },
        reverse=True,
    )

    if fiscal_year is None:
        fiscal_year = (
            fiscal_year_options[0]
            if fiscal_year_options
            else _fiscal_year_from_date(dt.date.today())
        )

    selected_farms = _normalise_farms(farms)

    selected_rows = [
        r
        for r in all_rows
        if r.farm in selected_farms and r.statement_month
    ]
    by_month: dict[dt.date, list[MilkStatement]] = {}
    for r in selected_rows:
        by_month.setdefault(r.statement_month, []).append(r)

    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    months = _iter_month_starts(fy_start, fy_end)
    rows = [_month_row(by_month.get(m, []), m) for m in months]

    year_records = [r for m in months for r in by_month.get(m, [])]
    total = {
        "month_label": "Total / Avg",
        "statement_month": None,
        "is_total": True,
        **_combine(year_records),
    }

    multi = len(selected_farms) > 1
    if multi:
        farm_label = " + ".join(selected_farms) + " — weighted average"
    else:
        farm_label = _FARM_LABELS.get(selected_farms[0], selected_farms[0])

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_label": f"Apr {fiscal_year - 1} – Mar {fiscal_year}",
        "fiscal_year_options": fiscal_year_options,
        "farms": selected_farms,
        "farm_label": farm_label,
        "is_weighted": multi,
        "rows": rows,
        "total": total,
        "months_with_data": sum(1 for r in rows if r.get("has_data")),
    }
