"""Import genomic evaluation results from OneDrive.

Designed for a weekly Render cron (genomics change infrequently):

    python scripts/import_genomic_results.py

Skips download/replace when the newest workbook fingerprint is unchanged.
Use ``--force`` to reload anyway.
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
from app.services.genomic_import import import_genomic_results
from app.services.graph_onedrive import graph_is_configured


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description="Import genomic results from OneDrive (Genomic Results folder)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload even when the source file fingerprint is unchanged.",
    )
    args = parser.parse_args()

    if not graph_is_configured():
        print(
            "Genomic import is not configured. "
            "Set Graph API variables or LOCAL_HERD_EXPORT_DIR.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    init_db()
    db = SessionLocal()
    try:
        print("Step: importing genomic results…", flush=True)
        result = import_genomic_results(db, force=args.force)
        if result.get("skipped"):
            print(
                f"Skipped genomic results (unchanged): {result.get('source_file')} "
                f"({result.get('rows_imported', 0):,} rows already loaded)",
                flush=True,
            )
        else:
            print(
                f"Imported {result.get('rows_imported', 0):,} genomic result rows "
                f"from {result.get('source_file')}",
                flush=True,
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(
            f"Genomic import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
