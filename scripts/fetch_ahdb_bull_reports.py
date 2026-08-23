"""Download AHDB Holstein genomic, proven, and top-international bull lists.

Uses AHDB's public table API (same source as the on-page tables / Download).
For on-farm evaluation and comparison only.

    python scripts/fetch_ahdb_bull_reports.py
    python scripts/fetch_ahdb_bull_reports.py --out data/ahdb --only genomic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.ahdb_bulls import (
    REPORTS,
    AhdbBullsError,
    fetch_reports,
    import_reports,
    write_csv,
)


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Fetch AHDB Holstein genomic, proven, and international bull lists as CSV."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_PROJECT_ROOT / "data" / "ahdb",
        help="Output directory (default: data/ahdb).",
    )
    parser.add_argument(
        "--only",
        choices=sorted(REPORTS),
        action="append",
        help="Fetch only this report (repeatable). Default: both.",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Write CSVs only; do not import into the local database.",
    )
    args = parser.parse_args()

    try:
        reports = fetch_reports(keys=args.only)
    except AhdbBullsError as exc:
        print(f"AHDB fetch failed: {exc}", file=sys.stderr, flush=True)
        return 1

    for report in reports:
        dest = write_csv(report, args.out / REPORTS[report.key]["filename"])
        print(f"{report.label}: {len(report.rows):,} bulls -> {dest}", flush=True)

    if not args.skip_db:
        init_db()
        db = SessionLocal()
        try:
            result = import_reports(db, reports)
            print(
                f"Imported {result['rows_imported']:,} bulls into the database",
                flush=True,
            )
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
