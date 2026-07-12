"""Rebuild stock valuation and accrual snapshots without a full herd import.

Run on Render after deploy (or locally) to populate snapshot tables:
  python scripts/rebuild_stock_snapshots.py

Tip: pause traffic or run when the site is quiet — this shares the web
service's 512MB RAM. Rebuilds one farm at a time to reduce peak memory.
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.stock_accruals import rebuild_stock_accrual_snapshots
from app.services.stock_valuations import rebuild_stock_valuation_snapshots


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        print("Rebuilding stock valuation snapshots (one farm at a time)…", flush=True)
        valuation_stats = rebuild_stock_valuation_snapshots(db)
        print(
            f"  {valuation_stats['rows_written']:,} rows "
            f"(anchor {valuation_stats.get('anchor_import_timestamp') or 'n/a'})",
            flush=True,
        )
        db.close()
        gc.collect()
        db = SessionLocal()

        print("Rebuilding stock accrual snapshots (farm × group)…", flush=True)
        accrual_stats = rebuild_stock_accrual_snapshots(db)
        print(
            f"  {accrual_stats['rows_written']:,} rows "
            f"(anchor {accrual_stats.get('anchor_import_timestamp') or 'n/a'})",
            flush=True,
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
