#!/usr/bin/env python3
"""Import feed rate data from Feedlync (for Render cron or manual runs).

On the 2nd of the month (Europe/London), also import last month's Feed Usage
Loaded Mixes totals.

Usage:
  python scripts/import_feed_data.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.feed_rate_import import import_feed_rate
from app.services.feed_usage import import_previous_month_usage_if_due

_UK = ZoneInfo("Europe/London")


def main() -> int:
    init_db()
    db = SessionLocal()
    failed = False
    try:
        try:
            result = import_feed_rate(db)
            print(
                f"Imported {result['rows_imported']:,} feed rate rows "
                f"({len(result['ration_names'])} rations)"
            )
            if result.get("latest_import"):
                print(f"Latest import: {result['latest_import']}")
        except Exception as exc:
            print(f"Feed rate import failed: {exc}", file=sys.stderr)
            failed = True

        today = datetime.now(_UK).date()
        try:
            usage = import_previous_month_usage_if_due(db, today=today)
        except Exception as exc:
            print(f"Feed usage import failed: {exc}", file=sys.stderr)
            return 1
        if usage:
            print(
                f"Imported {usage['rows_imported']:,} feed usage ingredients "
                f"for {usage['month']}"
            )
            if usage.get("latest_import"):
                print(f"Usage import: {usage['latest_import']}")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
