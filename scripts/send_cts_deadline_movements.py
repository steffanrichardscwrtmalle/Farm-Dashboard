"""Send pending BCMS movements that are on their UK reporting deadline day.

Render cron runs at 20:10 and 21:10 UTC (one of those is 9:10pm UK year-round).
There is no time gate on the default command, so a dashboard Trigger sends now.

Only sends animals whose Days Since Event equals the deadline (births 17,
sales/move-ons 3, deaths 7). Already-reported rows are not pending. Overdue
rows are left for manual send on Record Movements.

    python scripts/send_cts_deadline_movements.py
    python scripts/send_cts_deadline_movements.py --farm CM --dry-run
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
from app.services.cts_submit import (
    CtsSubmitError,
    record_deadline_day_receipt,
    send_deadline_day_movements,
)

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
        description="Send pending BCMS movements that are on their reporting deadline day."
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
            "Exit 0 without sending outside those hours. Example: 21"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List deadline-day movements without submitting them to BCMS.",
    )
    parser.add_argument(
        "--record-receipt",
        type=str,
        default="",
        help=(
            "Do not submit. Mark today's deadline-day pending rows as already "
            "sent with this BCMS receipt (after a validation timeout)."
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
                f"Skipping deadline send — UK time is {now_uk:%Y-%m-%d %H:%M %Z}, "
                f"allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0
        print(
            f"UK time gate passed ({now_uk:%Y-%m-%d %H:%M %Z}); "
            "sending deadline-day movements…",
            flush=True,
        )

    status = cts_status()
    if not status.get("ddts_configured"):
        print(
            "CTS send is not configured. Set CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if not status.get("ready_farms"):
        print(
            "CTS send has no ready farms. Set CTWS username/password/holding per farm.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    farms = [f.strip().upper() for f in (args.farms or []) if f and f.strip()] or None

    init_db()
    db = SessionLocal()
    try:
        receipt = (args.record_receipt or "").strip()
        if receipt:
            result = record_deadline_day_receipt(db, receipt=receipt, farms=farms)
        else:
            result = send_deadline_day_movements(
                db, farms=farms, dry_run=bool(args.dry_run)
            )
        for farm_result in result.get("results") or []:
            ids = farm_result.get("ids") or []
            id_bit = f"; ids={', '.join(str(i) for i in ids)}" if ids else ""
            print(
                f"{farm_result.get('farm')}: "
                f"due={farm_result.get('due_count', 0)} "
                f"accepted={farm_result.get('accepted_count', 0)} "
                f"rejected={farm_result.get('rejected_count', 0)} "
                f"{farm_result.get('message') or ''}"
                f"{id_bit}",
                flush=True,
            )
        if not result.get("results"):
            print("No CTS-ready farms to send from.", flush=True)
        if not result.get("ok"):
            print("Deadline send completed with errors.", file=sys.stderr, flush=True)
            return 1
        print("Deadline send complete.", flush=True)
        return 0
    except CtsSubmitError as exc:
        db.rollback()
        print(f"Deadline send failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except CtsError as exc:
        db.rollback()
        print(f"Deadline send failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(
            f"Deadline send failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
