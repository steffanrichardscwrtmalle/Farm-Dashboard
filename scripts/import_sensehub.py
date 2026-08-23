#!/usr/bin/env python3
"""Import SenseHub heat / health reports (for Render cron or manual runs).

Usage:
  python scripts/import_sensehub.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.sensehub_import import import_sensehub


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        result = import_sensehub(db)
        print(
            f"Imported {result['reports_imported']} SenseHub reports "
            f"({result['rows_imported']} rows)"
        )
        if result.get("farm_name"):
            print(f"Farm: {result['farm_name']} ({result.get('farm_id')})")
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
