"""Sync cattle on holding from BCMS CTS (DDTS / CTWS).

Designed for Render cron at 2am UK time. Schedule hourly and pass
``--only-at-uk-hours 2`` so BST/GMT stay correct year-round:

    python scripts/sync_cts_holding.py --only-at-uk-hours 2

Manual run (both farms that are configured):

    python scripts/sync_cts_holding.py
    python scripts/sync_cts_holding.py --farm CM
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.cts_client import CtsError, cts_status
from app.services.cts_reconcile import sync_farms

_UK = ZoneInfo("Europe/London")


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def _parse_hours(raw: str) -> set[int]:
    hours: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hour = int(part)
        if hour < 0 or hour > 23:
            raise ValueError(f"Invalid hour {hour!r} (expected 0-23)")
        hours.add(hour)
    return hours


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description="Sync CTS cattle-on-holding snapshots for configured farms."
    )
    parser.add_argument(
        "--farm",
        action="append",
        dest="farms",
        default=[],
        help="Farm code (CM and/or GAD). Repeatable. Default: all ready farms.",
    )
    parser.add_argument(
        "--only-at-uk-hours",
        type=str,
        default="",
        help=(
            "Comma-separated UK local hours (Europe/London) when this job may run. "
            "Exit 0 without syncing outside those hours. Example: 2"
        ),
    )
    args = parser.parse_args()

    if args.only_at_uk_hours.strip():
        try:
            allowed = _parse_hours(args.only_at_uk_hours)
        except ValueError as exc:
            print(f"Invalid --only-at-uk-hours: {exc}", file=sys.stderr, flush=True)
            return 2
        now_uk = datetime.now(_UK)
        if now_uk.hour not in allowed:
            print(
                f"Skipping CTS sync — UK time is {now_uk:%Y-%m-%d %H:%M %Z}, "
                f"allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0
        print(
            f"UK time gate passed ({now_uk:%Y-%m-%d %H:%M %Z}); syncing CTS…",
            flush=True,
        )

    status = cts_status()
    if not status.get("ddts_configured"):
        print(
            "CTS sync is not configured. Set CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if not status.get("ready_farms"):
        print(
            "CTS sync has no ready farms. Set CTWS username/password/holding per farm.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    farms = [f.strip().upper() for f in (args.farms or []) if f and f.strip()] or None

    init_db()
    db = SessionLocal()
    try:
        result = sync_farms(db, farms=farms, source="cron")
        for warning in result.get("warnings") or []:
            print(f"Warning: {warning}", flush=True)
        for farm_result in result.get("results") or []:
            print(
                f"{farm_result.get('farm')}: "
                f"cts={farm_result.get('cts_count')} "
                f"matched={farm_result.get('matched_count')} "
                f"cts_only={farm_result.get('cts_only_count')} "
                f"inventory_only={farm_result.get('inventory_only_count')}",
                flush=True,
            )
        if not result.get("ok"):
            print("CTS sync completed with errors.", file=sys.stderr, flush=True)
            return 1
        print("CTS sync complete.", flush=True)
        return 0
    except CtsError as exc:
        db.rollback()
        print(f"CTS sync failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(
            f"CTS sync failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
