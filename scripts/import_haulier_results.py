"""Import milk haulier collection reports from the emailed XLSX files.

Run on a schedule (Render cron), e.g. daily:
    python scripts/import_haulier_results.py

Reads XLSX attachments sent by HAULIER_SENDER from the configured mailbox(es)
(or from LOCAL_HAULIER_DIR in development) and upserts them into milk_collections.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.haulier_import import import_haulier_collections


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    init_db()
    db = SessionLocal()
    try:
        print("Step: importing milk haulier collections...", flush=True)
        stats = import_haulier_collections(db)
        print(
            f"Processed {stats['files_processed']} report(s), "
            f"skipped {stats['files_skipped']}; "
            f"inserted {stats['rows_inserted']:,}, updated {stats['rows_updated']:,} "
            f"({stats['rows_total']:,} loads).",
            flush=True,
        )
        for warning in stats.get("warnings") or []:
            print(f"WARNING: {warning}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - cron entry point: log and exit non-zero
        print(
            f"Haulier import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
