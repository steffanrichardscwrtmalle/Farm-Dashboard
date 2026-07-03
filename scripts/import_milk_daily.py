"""Daily cron: import haulier collections, NML results, and buyer statements from email.

Run on a schedule (Render cron), e.g. every morning:
    python scripts/import_milk_daily.py

By default scans the last 2 days of mail (yesterday + today). Override with
``--days`` or the ``MILK_CRON_LOOKBACK_DAYS`` environment variable.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import MILK_CRON_LOOKBACK_DAYS
from app.db import SessionLocal, init_db
from app.services.haulier_import import import_haulier_collections
from app.services.milk_statements_import import import_milk_statements
from app.services.nml_import import import_nml_results


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def _print_stats(label: str, stats: dict[str, Any]) -> None:
    print(
        f"{label}: processed {stats.get('files_processed', 0)} file(s), "
        f"skipped {stats.get('files_skipped', 0)}; "
        f"inserted {stats.get('rows_inserted', 0):,}, "
        f"updated {stats.get('rows_updated', 0):,} "
        f"({stats.get('rows_total', 0):,} rows).",
        flush=True,
    )
    for warning in stats.get("warnings") or []:
        print(f"WARNING [{label}]: {warning}", flush=True)
    mailbox_stats = stats.get("mailbox_stats") or {}
    for farm, info in mailbox_stats.items():
        print(
            f"  {farm}: {info.get('pdfs_found', 0)} PDF(s) from {info.get('mailbox', '?')}",
            flush=True,
        )


def _run_step(
    label: str,
    fn: Callable[[], dict[str, Any]],
    *,
    failures: list[str],
) -> None:
    print(f"Step: {label}…", flush=True)
    try:
        stats = fn()
        _print_stats(label, stats)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"FAILED [{label}]: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description="Import haulier collections, NML results, and milk statements from email."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=MILK_CRON_LOOKBACK_DAYS,
        help=f"Scan this many days of mail (default: {MILK_CRON_LOOKBACK_DAYS}).",
    )
    args = parser.parse_args()
    days = args.days if args.days and args.days > 0 else MILK_CRON_LOOKBACK_DAYS

    print(f"Milk daily import — scanning last {days} day(s) of mail.", flush=True)

    init_db()
    db = SessionLocal()
    failures: list[str] = []
    try:
        _run_step(
            "Haulier collections",
            lambda: import_haulier_collections(db, days=days),
            failures=failures,
        )
        _run_step(
            "NML results",
            lambda: import_nml_results(db, days=days),
            failures=failures,
        )
        _run_step(
            "Milk statements",
            lambda: import_milk_statements(db, days=days),
            failures=failures,
        )
    finally:
        db.close()

    if failures:
        print(f"Finished with {len(failures)} failure(s).", file=sys.stderr, flush=True)
        return 1
    print("Milk daily import complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
