"""Run every production cron-equivalent import for the offline / local app."""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import MILK_CRON_LOOKBACK_DAYS
from app.services.cattle_sales_import import import_cattle_sales
from app.services.cts_reconcile import sync_farms
from app.services.haulier_import import import_haulier_collections
from app.services.herd_full_import import refresh_herd_from_onedrive
from app.services.milk_statements_import import import_milk_statements
from app.services.nml_import import import_nml_results
from app.services.parlour_email_import import import_parlour_milk_flow

logger = logging.getLogger(__name__)


def _run_step(
    name: str,
    fn: Callable[[], dict[str, Any]],
    *,
    results: dict[str, Any],
    failures: list[str],
) -> None:
    logger.info("Offline refresh-all: starting %s", name)
    try:
        results[name] = fn()
        logger.info("Offline refresh-all: finished %s", name)
    except Exception as exc:  # noqa: BLE001 - keep remaining cron steps running
        message = f"{type(exc).__name__}: {exc}"
        failures.append(f"{name}: {message}")
        results[name] = {"error": message}
        logger.exception("Offline refresh-all: %s failed", name)


def refresh_all_cron_jobs(
    db: Session,
    *,
    days: int | None = None,
) -> dict[str, Any]:
    """Mirror Render cron jobs: milk/email imports, herd/OneDrive, CTS holding.

    Each step is isolated so one failure does not block the rest. Intended for
    the offline (non-production) app only.
    """
    lookback = days if days is not None and days > 0 else MILK_CRON_LOOKBACK_DAYS
    results: dict[str, Any] = {}
    failures: list[str] = []

    _run_step(
        "haulier",
        lambda: import_haulier_collections(db, days=lookback),
        results=results,
        failures=failures,
    )
    _run_step(
        "nml",
        lambda: import_nml_results(db, days=lookback),
        results=results,
        failures=failures,
    )
    _run_step(
        "milk_statements",
        lambda: import_milk_statements(db, days=lookback),
        results=results,
        failures=failures,
    )
    _run_step(
        "cattle_sales",
        lambda: import_cattle_sales(db, days=lookback),
        results=results,
        failures=failures,
    )
    _run_step(
        "parlour",
        lambda: import_parlour_milk_flow(db, days=lookback),
        results=results,
        failures=failures,
    )
    # Covers farm-dashboard-import-dc305, inventory-import, and genomic-import.
    _run_step(
        "herd_onedrive",
        lambda: refresh_herd_from_onedrive(db, include_genomics=True),
        results=results,
        failures=failures,
    )
    _run_step(
        "cts",
        lambda: sync_farms(db, source="offline-refresh"),
        results=results,
        failures=failures,
    )

    return {
        "ok": not failures,
        "days": lookback,
        "failures": failures,
        "steps": results,
    }
