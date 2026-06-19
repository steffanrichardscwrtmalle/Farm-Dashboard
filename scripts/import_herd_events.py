"""
Import cow events from OneDrive CSV exports into the database.

For Render cron (daily):
  python scripts/import_herd_events.py

Requires Graph API env vars or LOCAL_HERD_EXPORT_DIR for local synced files.
Optional: IMPORT_API_KEY if calling the HTTP API instead of running in-process.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.herd_events_import import import_cow_events


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        result = import_cow_events(db)
        print(
            f"Imported {result['rows_imported']:,} cow events "
            f"(CM: {result['farm_counts'].get('CM', 0):,}, "
            f"GAD: {result['farm_counts'].get('GAD', 0):,})"
        )
        if result.get("latest_event_date"):
            print(f"Latest event date: {result['latest_event_date']}")
        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
