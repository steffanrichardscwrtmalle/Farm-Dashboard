"""
Import herd data from OneDrive CSV exports into the database.

For Render cron (weekly):
  python scripts/import_herd_events.py

Imports cow events, inventory, genomic results, and birth records.
Requires Graph API env vars or LOCAL_HERD_EXPORT_DIR for local synced files.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.genomic_import import import_genomic_results
from app.services.herd_birth_import import import_herd_births
from app.services.herd_events_import import import_cow_events
from app.services.herd_inventory_import import import_herd_inventory
from app.services.stock_valuations import rebuild_stock_valuation_snapshots


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        events = import_cow_events(db)
        print(
            f"Imported {events['rows_imported']:,} cow events "
            f"(CM: {events['farm_counts'].get('CM', 0):,}, "
            f"GAD: {events['farm_counts'].get('GAD', 0):,})"
        )
        if events.get("latest_event_date"):
            print(f"Latest event date: {events['latest_event_date']}")
        purchase_stats = events.get("purchase_stats") or {}
        if purchase_stats.get("excluded_count", 0) > 0:
            print(
                f"Excluded {purchase_stats['excluded_count']:,} GAD purchases "
                f"(UK752261* with EDAT before Apr-2025)"
            )
        if events.get("duplicate_fresh_dropped", 0) > 0:
            print(
                f"Dropped {events['duplicate_fresh_dropped']:,} duplicate FRESH event rows"
            )
        if events.get("duplicate_exit_dropped", 0) > 0:
            print(
                f"Dropped {events['duplicate_exit_dropped']:,} duplicate SOLD/DIED event rows"
            )

        inventory = import_herd_inventory(db)
        print(
            f"Imported {inventory['rows_imported']:,} inventory rows "
            f"(CM: {inventory['farm_counts'].get('CM', 0):,}, "
            f"GAD: {inventory['farm_counts'].get('GAD', 0):,})"
        )

        genomic = import_genomic_results(db)
        print(f"Imported {genomic['rows_imported']:,} genomic result rows")

        births = import_herd_births(db)
        print(
            f"Imported {births['rows_imported']:,} birth records "
            f"(CM: {births['farm_counts'].get('CM', 0):,}, "
            f"GAD: {births['farm_counts'].get('GAD', 0):,})"
        )
        if births.get("duplicate_rows_dropped", 0) > 0:
            by_farm = births.get("duplicate_rows_dropped_by_farm", {})
            farm_detail = ", ".join(f"{farm}: {count:,}" for farm, count in sorted(by_farm.items()))
            print(
                f"Dropped {births['duplicate_rows_dropped']:,} duplicate birth rows"
                + (f" ({farm_detail})" if farm_detail else "")
            )
        if births.get("latest_birth_date"):
            print(f"Latest birth date: {births['latest_birth_date']}")

        valuation_stats = rebuild_stock_valuation_snapshots(db)
        print(
            f"Rebuilt stock valuation snapshots: {valuation_stats['rows_written']:,} rows "
            f"for anchor {valuation_stats.get('anchor_import_timestamp') or 'n/a'}"
        )

        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
