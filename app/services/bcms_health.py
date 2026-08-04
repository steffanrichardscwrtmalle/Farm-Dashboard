"""Home-dashboard BCMS / CTS reconcile health status."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, StockPurchaseAnimal
from app.services.cts_client import normalize_cts_etag
from app.services.cts_reconcile import reconcile_farm

HealthLevel = Literal["green", "yellow", "red", "unknown"]


def _purchases_by_etag(db: Session, farm: str) -> dict[str, dt.date]:
    out: dict[str, dt.date] = {}
    rows = db.scalars(
        select(StockPurchaseAnimal)
        .where(StockPurchaseAnimal.farm == farm.upper())
        .order_by(StockPurchaseAnimal.edat.desc())
    ).all()
    for row in rows:
        key = normalize_cts_etag(row.etag)
        if key and row.edat is not None and key not in out:
            out[key] = row.edat
    return out


def _age_days(value: dt.date | None, *, as_of: dt.date) -> int | None:
    if value is None:
        return None
    days = (as_of - value).days
    return days if days >= 0 else None


def _farm_mismatch_ages(
    db: Session,
    farm: str,
    recon: dict[str, Any],
    *,
    as_of: dt.date,
) -> list[dict[str, Any]]:
    """Classify each CTS↔inventory mismatch with an event age in days."""
    purchases = _purchases_by_etag(db, farm)
    items: list[dict[str, Any]] = []

    for row in recon.get("cts_only") or []:
        days = row.get("days_since_exit")
        if days is not None:
            items.append(
                {
                    "farm": farm,
                    "etag": row.get("etag"),
                    "kind": "exit",
                    "days": int(days),
                    "explainable": True,
                }
            )
        else:
            items.append(
                {
                    "farm": farm,
                    "etag": row.get("etag"),
                    "kind": "cts_only_unexplained",
                    "days": None,
                    "explainable": False,
                }
            )

    for row in recon.get("inventory_only") or []:
        etag = row.get("etag") or ""
        purchase_date = purchases.get(etag)
        if purchase_date is not None:
            days = _age_days(purchase_date, as_of=as_of)
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "move_on",
                    "days": days,
                    "explainable": days is not None,
                }
            )
            continue
        days = row.get("age_days")
        if days is not None:
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "birth",
                    "days": int(days),
                    "explainable": True,
                }
            )
        else:
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "inventory_only_unexplained",
                    "days": None,
                    "explainable": False,
                }
            )

    return items


def _status_from_ages(items: list[dict[str, Any]]) -> tuple[HealthLevel, str, int | None]:
    """Return (level, label, max_days)."""
    if not items:
        return "green", "Healthy", None

    unexplained = [i for i in items if not i.get("explainable") or i.get("days") is None]
    if unexplained:
        return "red", "Unhealthy", None

    max_days = max(int(i["days"]) for i in items)
    if max_days <= 1:
        # Perfect-enough: only sold / died / born / moved on today or yesterday.
        return "green", "Healthy", max_days
    if max_days == 2:
        return "yellow", "Attention", max_days
    return "red", "Unhealthy", max_days


def get_bcms_health(
    db: Session,
    *,
    farms: list[str] | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Aggregate BCMS reconcile health for the home dashboard widget."""
    today = as_of or dt.date.today()
    farm_keys = [f.upper() for f in (farms or list(HERD_FARM_OPTIONS))]
    farm_keys = [f for f in farm_keys if f in HERD_FARM_OPTIONS]

    farm_summaries: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    any_snapshot = False

    for farm in farm_keys:
        recon = reconcile_farm(db, farm)
        items = _farm_mismatch_ages(db, farm, recon, as_of=today)
        level, label, max_days = _status_from_ages(items)
        cts_count = int(recon.get("cts_count") or 0)
        inv_count = int(recon.get("inventory_count") or 0)
        if cts_count > 0:
            any_snapshot = True
        farm_summaries.append(
            {
                "farm": farm,
                "status": level,
                "label": label,
                "max_days": max_days,
                "mismatch_count": len(items),
                "cts_only_count": recon.get("cts_only_count") or 0,
                "inventory_only_count": recon.get("inventory_only_count") or 0,
                "matched_count": recon.get("matched_count") or 0,
                "synced_at": recon.get("synced_at"),
                "has_snapshot": cts_count > 0,
                "inventory_count": inv_count,
            }
        )
        all_items.extend(items)

    if not any_snapshot and not all_items:
        # No CTS data pulled yet — don't pretend everything is fine.
        overall: HealthLevel = "unknown"
        overall_label = "No CTS sync"
        overall_max: int | None = None
    else:
        overall, overall_label, overall_max = _status_from_ages(all_items)

    return {
        "status": overall,
        "label": overall_label,
        "max_days": overall_max,
        "mismatch_count": len(all_items),
        "href": "/bcms/record-movements",
        "farms": farm_summaries,
        "as_of": today.isoformat(),
    }
