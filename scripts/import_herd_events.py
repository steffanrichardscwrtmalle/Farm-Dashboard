"""
Import herd data from OneDrive CSV exports into the database.

For Render cron (weekly):
  python scripts/import_herd_events.py

Imports cow events, inventory, genomic results, and birth records.
Requires Graph API env vars or LOCAL_HERD_EXPORT_DIR for local synced files.
"""

from __future__ import annotations

import gc
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.services.genomic_import import import_genomic_results
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


def main() -> int:
    # Cron pipes are block-buffered; force line buffering so logs are not lost on crash.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    init_db()
    db = SessionLocal()
    step = "starting"
    try:
        step = "cow events"
        _log("Step: importing cow events...")
        events = import_cow_events(db)
        _log(
            f"Imported {events['rows_imported']:,} cow events "
            f"(CM: {events['farm_counts'].get('CM', 0):,}, "
            f"GAD: {events['farm_counts'].get('GAD', 0):,})"
        )
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
        _log("Step: importing inventory...")
        inventory = import_herd_inventory(db)
        _log(
            f"Imported {inventory['rows_imported']:,} inventory rows "
            f"(CM: {inventory['farm_counts'].get('CM', 0):,}, "
            f"GAD: {inventory['farm_counts'].get('GAD', 0):,})"
        )
        _release_memory(db)

        step = "genomic results"
        _log("Step: importing genomic results...")
        # Genomic data is supplementary (Genetics pages only). A missing or broken
        # genomicresults.xlsx must not abort the core herd / valuation pipeline.
        try:
            genomic = import_genomic_results(db)
            _log(f"Imported {genomic['rows_imported']:,} genomic result rows")
        except FileNotFoundError as exc:
            db.rollback()
            _log(f"WARNING: skipped genomic results (file missing): {exc}")
        except Exception as exc:  # noqa: BLE001 - keep core pipeline running
            db.rollback()
            _log(f"WARNING: skipped genomic results ({type(exc).__name__}): {exc}")
        _release_memory(db)

        step = "births"
        _log("Step: importing births...")
        births = import_herd_births(db)
        _log(
            f"Imported {births['rows_imported']:,} birth records "
            f"(CM: {births['farm_counts'].get('CM', 0):,}, "
            f"GAD: {births['farm_counts'].get('GAD', 0):,})"
        )
        if births.get("duplicate_rows_dropped", 0) > 0:
            by_farm = births.get("duplicate_rows_dropped_by_farm", {})
            farm_detail = ", ".join(f"{farm}: {count:,}" for farm, count in sorted(by_farm.items()))
            _log(
                f"Dropped {births['duplicate_rows_dropped']:,} duplicate birth rows"
                + (f" ({farm_detail})" if farm_detail else "")
            )
        if births.get("latest_birth_date"):
            _log(f"Latest birth date: {births['latest_birth_date']}")

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
