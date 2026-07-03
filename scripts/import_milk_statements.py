"""Import monthly milk buyer statement PDFs from email.

Run on a schedule (Render cron), e.g. monthly after statements arrive:
    python scripts/import_milk_statements.py

Daily cron (2-day lookback) also picks up new statements:
    python scripts/import_milk_daily.py
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.milk_statements_import import import_milk_statements


def main() -> int:
    parser = argparse.ArgumentParser(description="Import milk buyer statement PDFs from email.")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Scan this many days of mail (default: STATEMENTS_LOOKBACK_DAYS from config).",
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    init_db()
    db = SessionLocal()
    try:
        print("Step: importing milk buyer statements...", flush=True)
        stats = import_milk_statements(db, days=args.days)
        print(
            f"Processed {stats['files_processed']} statement(s), "
            f"skipped {stats['files_skipped']}; "
            f"inserted {stats['rows_inserted']:,}, updated {stats['rows_updated']:,} "
            f"({stats['rows_total']:,} months).",
            flush=True,
        )
        for warning in stats.get("warnings") or []:
            print(f"WARNING: {warning}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            f"Milk statements import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
