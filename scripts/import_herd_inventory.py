"""Import herd inventory CSVs from OneDrive when the files have changed.

Designed for a frequent Render cron (inventory is lighter than full herd import):

    python scripts/import_herd_inventory.py

Each farm (CM / GAD) is checked independently — only changed inventory files
are downloaded and replaced. Use ``--force`` to reload both anyway.
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
from app.services.graph_onedrive import graph_is_configured
from app.services.herd_inventory_import import import_herd_inventory


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description=(
            "Import CM/GAD inventory CSVs from OneDrive "
            "(per-farm skip if that file is unchanged)."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload both farms even when source fingerprints are unchanged.",
    )
    args = parser.parse_args()

    if not graph_is_configured():
        print(
            "Herd inventory import is not configured. "
            "Set Graph API variables or LOCAL_HERD_EXPORT_DIR.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    init_db()
    db = SessionLocal()
    try:
        print("Step: checking / importing herd inventory…", flush=True)
        result = import_herd_inventory(db, force=args.force)
        imported = result.get("farms_imported") or []
        skipped = result.get("farms_skipped") or []
        if result.get("skipped"):
            print(
                "Skipped herd inventory (unchanged): "
                + ", ".join(skipped or result.get("source_files") or []),
                flush=True,
            )
        else:
            bits = [
                f"Imported {result.get('rows_imported', 0):,} inventory rows"
            ]
            if imported:
                bits.append(f"updated={','.join(imported)}")
            if skipped:
                bits.append(f"skipped={','.join(skipped)}")
            counts = result.get("farm_counts") or {}
            bits.append(
                f"(CM: {counts.get('CM', 0):,}, GAD: {counts.get('GAD', 0):,})"
            )
            print(" ".join(bits), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(
            f"Herd inventory import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
