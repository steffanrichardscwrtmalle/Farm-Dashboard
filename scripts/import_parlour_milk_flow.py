"""Import parlour milk-flow reports from Dataflow email.

Designed for Render cron. To hit 1am / 9am / 5pm UK time year-round (including
BST), schedule this hourly and pass ``--only-at-uk-hours 1,9,17`` so the job
no-ops outside those London-local hours:

    python scripts/import_parlour_milk_flow.py --only-at-uk-hours 1,9,17

Manual / forced run (ignores the hour gate):

    python scripts/import_parlour_milk_flow.py --days 2
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

from app.config import MILK_CRON_LOOKBACK_DAYS, PARLOUR_LOOKBACK_DAYS
from app.db import SessionLocal, init_db
from app.services.parlour_email_import import import_parlour_milk_flow, parlour_is_configured

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

    default_days = MILK_CRON_LOOKBACK_DAYS or min(2, PARLOUR_LOOKBACK_DAYS)
    parser = argparse.ArgumentParser(
        description="Import parlour milk-flow reports from Dataflow email."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=default_days,
        help=f"Scan this many days of mail (default: {default_days}).",
    )
    parser.add_argument(
        "--only-at-uk-hours",
        type=str,
        default="",
        help=(
            "Comma-separated UK local hours (Europe/London) when this job may run. "
            "Exit 0 without importing outside those hours. Example: 1,9,17"
        ),
    )
    args = parser.parse_args()
    days = args.days if args.days and args.days > 0 else default_days

    if args.only_at_uk_hours.strip():
        try:
            allowed = _parse_hours(args.only_at_uk_hours)
        except ValueError as exc:
            print(f"Invalid --only-at-uk-hours: {exc}", file=sys.stderr, flush=True)
            return 2
        now_uk = datetime.now(_UK)
        if now_uk.hour not in allowed:
            print(
                f"Skipping parlour import — UK time is {now_uk:%Y-%m-%d %H:%M %Z}, "
                f"allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0
        print(
            f"UK time gate passed ({now_uk:%Y-%m-%d %H:%M %Z}); "
            f"scanning last {days} day(s) of mail.",
            flush=True,
        )
    else:
        print(f"Parlour milk-flow import — scanning last {days} day(s) of mail.", flush=True)

    if not parlour_is_configured():
        print(
            "Parlour import is not configured "
            "(set Graph API vars or LOCAL_PARLOUR_DIR).",
            file=sys.stderr,
            flush=True,
        )
        return 1

    init_db()
    db = SessionLocal()
    try:
        print("Step: Parlour milk-flow…", flush=True)
        stats = import_parlour_milk_flow(db, days=days)
        print(
            f"Parlour milk-flow: processed {stats.get('files_processed', 0)} file(s), "
            f"imported {stats.get('shifts_imported', stats.get('rows_inserted', 0))} "
            f"shift(s).",
            flush=True,
        )
        for warning in stats.get("warnings") or []:
            print(f"WARNING: {warning}", flush=True)
        for skipped in stats.get("skipped_files") or []:
            print(f"SKIPPED: {skipped}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1
    finally:
        db.close()

    print("Parlour milk-flow import complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
