#!/usr/bin/env python3
"""Import feed rate data from Feedlync (for Render cron or manual runs).

Usage:
  python scripts/import_feed_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.feed_rate_import import import_feed_rate


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        result = import_feed_rate(db)
        print(
            f"Imported {result['rows_imported']:,} feed rate rows "
            f"({len(result['ration_names'])} rations)"
        )
        if result.get("latest_import"):
            print(f"Latest import: {result['latest_import']}")
        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
