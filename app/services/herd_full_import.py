"""Full OneDrive / local-folder herd refresh (events, inventory, births, snapshots)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.genomic_import import import_genomic_results
from app.services.graph_onedrive import graph_is_configured
from app.services.herd_birth_import import import_herd_births
from app.services.herd_events_import import import_cow_events
from app.services.herd_inventory_import import import_herd_inventory
from app.services.stock_accruals import rebuild_stock_accrual_snapshots
from app.services.stock_valuations import rebuild_stock_valuation_snapshots

logger = logging.getLogger(__name__)


def refresh_herd_from_onedrive(
    db: Session,
    *,
    include_genomics: bool = True,
) -> dict[str, Any]:
    """Import all OneDrive herd reports and rebuild stock snapshots.

    Mirrors ``scripts/import_herd_events.py``, and optionally refreshes genomic
    results (force) so a local offline DB can catch up with production sources.
    """
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set LOCAL_HERD_EXPORT_DIR "
            "(offline OneDrive folder) or Graph API variables."
        )

    logger.info("OneDrive herd refresh: starting cow events")
    events = import_cow_events(db)
    logger.info(
        "OneDrive herd refresh: events=%s", events.get("rows_imported")
    )

    logger.info("OneDrive herd refresh: starting inventory")
    inventory = import_herd_inventory(db)
    logger.info(
        "OneDrive herd refresh: inventory=%s", inventory.get("rows_imported")
    )

    logger.info("OneDrive herd refresh: starting births")
    births = import_herd_births(db)
    logger.info(
        "OneDrive herd refresh: births=%s", births.get("rows_imported")
    )

    logger.info("OneDrive herd refresh: rebuilding stock valuations")
    valuations = rebuild_stock_valuation_snapshots(db)
    logger.info(
        "OneDrive herd refresh: valuations=%s", valuations.get("rows_written")
    )

    logger.info("OneDrive herd refresh: rebuilding stock accruals")
    accruals = rebuild_stock_accrual_snapshots(db)
    logger.info(
        "OneDrive herd refresh: accruals=%s", accruals.get("rows_written")
    )

    genomics: dict[str, Any] | None = None
    if include_genomics:
        logger.info("OneDrive herd refresh: starting genomics")
        genomics = import_genomic_results(db, force=True)
        logger.info(
            "OneDrive herd refresh: genomics=%s",
            genomics.get("rows_imported"),
        )

    return {
        "ok": True,
        "events": {
            "rows_imported": events.get("rows_imported"),
            "farm_counts": events.get("farm_counts"),
            "latest_event_date": events.get("latest_event_date"),
        },
        "inventory": {
            "rows_imported": inventory.get("rows_imported"),
            "farm_counts": inventory.get("farm_counts"),
        },
        "births": {
            "rows_imported": births.get("rows_imported"),
            "farm_counts": births.get("farm_counts"),
            "latest_birth_date": births.get("latest_birth_date"),
        },
        "valuations": {
            "rows_written": valuations.get("rows_written"),
            "anchor_import_timestamp": valuations.get("anchor_import_timestamp"),
        },
        "accruals": {
            "rows_written": accruals.get("rows_written"),
            "anchor_import_timestamp": accruals.get("anchor_import_timestamp"),
        },
        "genomics": genomics,
    }
