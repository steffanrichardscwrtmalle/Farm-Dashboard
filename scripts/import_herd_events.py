"""
Import herd data from OneDrive CSV exports into the database.

For Render cron (weekly):
  python scripts/import_herd_events.py

Imports cow events, inventory, and birth records.
Requires Graph API env vars or LOCAL_HERD_EXPORT_DIR for local synced files.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.herd_birth_import import import_herd_births
from app.services.herd_events_import import import_cow_events
from app.services.herd_inventory_import import import_herd_inventory


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

        inventory = import_herd_inventory(db)
        print(
            f"Imported {inventory['rows_imported']:,} inventory rows "
            f"(CM: {inventory['farm_counts'].get('CM', 0):,}, "
            f"GAD: {inventory['farm_counts'].get('GAD', 0):,})"
        )

        births = import_herd_births(db)
        print(
            f"Imported {births['rows_imported']:,} birth records "
            f"(CM: {births['farm_counts'].get('CM', 0):,}, "
            f"GAD: {births['farm_counts'].get('GAD', 0):,})"
        )
        if births.get("latest_birth_date"):
            print(f"Latest birth date: {births['latest_birth_date']}")

        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
