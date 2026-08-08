"""
Import DC305 herd CSV exports from OneDrive into the database.

Render cron ``farm-dashboard-import-dc305`` (daily):
  python scripts/import_herd_events.py

Imports cow events, inventory, and birth records (skipping each farm whose
OneDrive file fingerprint is unchanged), then rebuilds stock snapshots when
anything changed. Use ``--force`` to reload all farms anyway.

Genomic results are imported separately via scripts/import_genomic_results.py.
Requires Graph API env vars or LOCAL_HERD_EXPORT_DIR for local synced files.
"""

from __future__ import annotations

import argparse
import gc
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.services.herd_birth_import import import_herd_births
from app.services.herd_events_import import import_cow_events
from app.services.herd_inventory_import import import_herd_inventory
from app.services.stock_accruals import rebuild_stock_accrual_snapshots
from app.services.stock_valuations import rebuild_stock_valuation_snapshots


def _log(message: str) -> None:
    """Print and flush immediately so cron logs show real progress before any crash."""
    print(message, flush=True)


def _release_memory(db: Session) -> None:
    """Drop cached ORM rows between heavy import steps (cron memory limit)."""
    db.expire_all()
    gc.collect()


def _log_import_result(label: str, result: dict) -> None:
    imported = result.get("farms_imported") or []
    skipped = result.get("farms_skipped") or []
    if result.get("skipped"):
        _log(f"Skipped {label} (unchanged): {', '.join(skipped) or 'all'}")
        return
    bits = [f"Imported {result.get('rows_imported', 0):,} {label}"]
    if imported:
        bits.append(f"updated={','.join(imported)}")
    if skipped:
        bits.append(f"skipped={','.join(skipped)}")
    counts = result.get("farm_counts") or {}
    bits.append(f"(CM: {counts.get('CM', 0):,}, GAD: {counts.get('GAD', 0):,})")
    _log(" ".join(bits))


def main() -> int:
    # Cron pipes are block-buffered; force line buffering so logs are not lost on crash.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Import CM/GAD events, inventory, and births from OneDrive "
            "(per-farm skip if that file is unchanged)."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload all farms even when source fingerprints are unchanged.",
    )
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    step = "starting"
    anything_changed = False
    try:
        step = "cow events"
        _log("Step: checking / importing cow events...")
        events = import_cow_events(db, force=args.force)
        _log_import_result("cow events", events)
        if not events.get("skipped"):
            anything_changed = True
        if events.get("latest_event_date"):
            _log(f"Latest event date: {events['latest_event_date']}")
        purchase_stats = events.get("purchase_stats") or {}
        if purchase_stats.get("excluded_count", 0) > 0:
            _log(
                f"Excluded {purchase_stats['excluded_count']:,} GAD purchases "
                f"(UK752261* with EDAT before Apr-2025)"
            )
        if events.get("duplicate_fresh_dropped", 0) > 0:
            _log(
                f"Dropped {events['duplicate_fresh_dropped']:,} duplicate FRESH event rows"
            )
        if events.get("duplicate_exit_dropped", 0) > 0:
            _log(
                f"Dropped {events['duplicate_exit_dropped']:,} duplicate SOLD/DIED event rows"
            )
        _release_memory(db)

        step = "inventory"
        _log("Step: checking / importing inventory...")
        inventory = import_herd_inventory(db, force=args.force)
        _log_import_result("inventory rows", inventory)
        if not inventory.get("skipped"):
            anything_changed = True
        _release_memory(db)

        step = "births"
        _log("Step: checking / importing births...")
        births = import_herd_births(db, force=args.force)
        _log_import_result("birth records", births)
        if not births.get("skipped"):
            anything_changed = True
        if births.get("duplicate_rows_dropped", 0) > 0:
            by_farm = births.get("duplicate_rows_dropped_by_farm", {})
            farm_detail = ", ".join(
                f"{farm}: {count:,}" for farm, count in sorted(by_farm.items())
            )
            _log(
                f"Dropped {births['duplicate_rows_dropped']:,} duplicate birth rows"
                + (f" ({farm_detail})" if farm_detail else "")
            )
        if births.get("latest_birth_date"):
            _log(f"Latest birth date: {births['latest_birth_date']}")

        if not anything_changed:
            _log("All herd sources unchanged; skipping stock snapshot rebuild.")
            return 0

        # Valuation rebuild loads all herd events; use a clean session first.
        db.close()
        gc.collect()
        db = SessionLocal()

        step = "stock valuations"
        _log("Step: rebuilding stock valuation snapshots...")
        valuation_stats = rebuild_stock_valuation_snapshots(db)
        _log(
            f"Rebuilt stock valuation snapshots: {valuation_stats['rows_written']:,} rows "
            f"for anchor {valuation_stats.get('anchor_import_timestamp') or 'n/a'}"
        )

        step = "stock accruals"
        _log("Step: rebuilding stock accrual snapshots...")
        accrual_stats = rebuild_stock_accrual_snapshots(db)
        _log(
            f"Rebuilt stock accrual snapshots: {accrual_stats['rows_written']:,} rows "
            f"for anchor {accrual_stats.get('anchor_import_timestamp') or 'n/a'}"
        )

        return 0
    except MemoryError:
        print(f"Import failed: out of memory during '{step}' step.", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1
    except Exception as exc:
        print(
            f"Import failed during '{step}' step: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
