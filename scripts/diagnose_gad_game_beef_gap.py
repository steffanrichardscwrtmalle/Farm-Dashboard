"""Diagnose GAD beef valuations vs accruals gap related to GAME/PATHWAY (JV) events."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.config import LOCAL_HERD_EXPORT_DIR
from app.db import SessionLocal, init_db
from app.models import CowEvent, HERD_FARM_OPTIONS, HerdInventory, STOCK_GROUP_BEEF
from app.services.graph_onedrive import download_herd_file
from app.services.stock_accruals import build_stock_accruals_report
from app.services.stock_valuations import (
    _build_profiles,
    _jv_beef_still_on_farm_count,
    _month_end,
    _resolve_state_at,
    animal_key,
    build_stock_valuations_report,
)

_JV_EVENTS = ("GAME", "PATHWAY")
_GAD_EVENTS_FILE = "DCEXPORTGAD/GADEVENTS.CSV"


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _first_game_dates(db, *, farm: str, through: dt.date) -> dict[str, dt.date]:
    rows = db.execute(
        select(CowEvent.etag, CowEvent.event_date)
        .where(CowEvent.farm == farm)
        .where(CowEvent.event.in_(_JV_EVENTS))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.event_date <= through)
        .where(CowEvent.etag.isnot(None))
        .order_by(CowEvent.event_date.asc(), CowEvent.id.asc())
    ).all()
    first: dict[str, dt.date] = {}
    for etag, event_date in rows:
        if etag not in first and event_date is not None:
            first[str(etag)] = event_date
    return first


def _game_etags_in_range(
    first_game: dict[str, dt.date],
    *,
    month_start: dt.date,
    range_end: dt.date,
) -> set[str]:
    return {
        etag
        for etag, game_date in first_game.items()
        if month_start <= game_date <= range_end
    }


def _csv_game_etags_missing_from_db(db, *, farm: str, month_start: dt.date) -> list[str]:
    if farm != "GAD" or not LOCAL_HERD_EXPORT_DIR:
        return []
    try:
        raw = download_herd_file(_GAD_EVENTS_FILE).decode("utf-8", errors="replace")
    except OSError:
        return []

    month_end = _month_end(month_start)
    missing: list[str] = []
    for line in raw.splitlines():
        if "GAME" not in line.upper():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 12:
            continue
        etag = parts[1].strip()
        date_str = parts[11].strip()
        try:
            event_date = dt.datetime.strptime(date_str, "%d/%m/%y").date()
        except ValueError:
            continue
        if not (month_start <= event_date <= month_end):
            continue
        if not etag:
            continue
        in_db = db.scalar(
            select(func.count())
            .select_from(CowEvent)
            .where(CowEvent.farm == farm)
            .where(CowEvent.etag == etag)
            .where(CowEvent.event == "GAME")
            .where(CowEvent.event_date == event_date)
        )
        if not in_db:
            missing.append(etag)
    return sorted(set(missing))


def _summarize_month(
    db,
    *,
    farm: str,
    month_start: dt.date,
    fiscal_year: int,
    anchor_ts,
    anchor_date: dt.date,
) -> None:
    month_end = _month_end(month_start)
    close_date = min(month_end, anchor_date)

    val_report = build_stock_valuations_report(
        db,
        farms=[farm],
        fiscal_year=fiscal_year,
        selected_month=month_start,
    )
    val_month = next(
        (m for m in val_report.get("months", []) if m["month_start"] == month_start.isoformat()),
        None,
    )
    val_beef = (
        val_month["totals"][farm]["categories"]["Beef"]["count"] if val_month else None
    )

    acc_report = build_stock_accruals_report(
        db,
        farms=[farm],
        stock_group="beef",
        fiscal_year=fiscal_year,
        month_from=month_start,
        month_to=month_end,
    )
    acc_row = next(
        (r for r in acc_report.get("rows", []) if r["month_start"] == month_start.isoformat()),
        None,
    )
    acc_closing = int(acc_row["closing"]) if acc_row else None

    _, profiles, _, exit_keys, _, jv_keys = _build_profiles(
        db,
        selected_farms=list(HERD_FARM_OPTIONS),
        anchor_ts=anchor_ts,
    )
    jv_beef = _jv_beef_still_on_farm_count(
        profiles, jv_keys, exit_keys, close_date, farm
    )

    first_game = _first_game_dates(db, farm=farm, through=month_end)
    game_calendar = _game_etags_in_range(
        first_game, month_start=month_start, range_end=month_end
    )
    game_to_close = _game_etags_in_range(
        first_game, month_start=month_start, range_end=close_date
    )
    game_after_close = game_calendar - game_to_close

    diff = (acc_closing - val_beef) if acc_closing is not None and val_beef is not None else None

    print(f"\n=== {farm} {month_start.strftime('%b-%Y')} ===")
    print(f"  Inventory anchor:     {anchor_date}")
    print(f"  Calendar month end:   {month_end}")
    print(f"  Valuation close:      {close_date}")
    print(f"  Accruals closing:     {acc_closing}")
    print(f"  Valuations beef:      {val_beef}")
    print(f"  Diff (acc - val):     {diff}")
    print(f"  JV beef adjustment:   {jv_beef}")
    print(f"  GAME ETAGs (calendar month, first GAME): {len(game_calendar)}")
    print(f"  GAME ETAGs (on/before close):            {len(game_to_close)}")
    if game_after_close:
        print(f"  GAME ETAGs after close, in month:       {len(game_after_close)}")
        for etag in sorted(game_after_close)[:10]:
            print(f"    {etag}  first_GAME={first_game[etag]}")
        if len(game_after_close) > 10:
            print(f"    ... and {len(game_after_close) - 10} more")

    cumulative_jv_beef: list[str] = []
    for key, jv_date in jv_keys.items():
        if key[0] != farm or jv_date > close_date:
            continue
        exit_date = exit_keys.get(key)
        if exit_date is not None and exit_date <= close_date:
            continue
        profile = profiles.get(key)
        if profile is None:
            continue
        state = _resolve_state_at(profile, jv_date)
        if state is not None and state["stock_group"] == STOCK_GROUP_BEEF:
            cumulative_jv_beef.append(profile.etag or str(key))

    print(f"  Cumulative JV beef ETAGs (<= close):     {len(cumulative_jv_beef)}")

    if diff is not None and jv_beef != diff:
        print(
            f"  JV beef vs acc-val diff:               {jv_beef - diff:+d} "
            f"(compare uses JV {jv_beef}; gap {diff})"
        )
        print(
            "  Note: when JV beef exceeds acc-val diff, accruals beef ledger "
            "may count fewer JV animals than valuations excludes."
        )

    missing_csv = _csv_game_etags_missing_from_db(db, farm=farm, month_start=month_start)
    if missing_csv:
        print(f"  GAME in CSV but not DB (sample month):   {len(missing_csv)}")
        for etag in missing_csv[:10]:
            print(f"    {etag}")
        if len(missing_csv) > 10:
            print(f"    ... and {len(missing_csv) - 10} more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--farm", default="GAD")
    parser.add_argument(
        "--month",
        action="append",
        dest="months",
        help="YYYY-MM (repeatable; default Apr–Jun 2026)",
    )
    parser.add_argument("--fiscal-year", type=int, default=2027)
    args = parser.parse_args()

    if args.months:
        month_starts = [
            _month_start(dt.date.fromisoformat(f"{m}-01" if len(m) == 7 else m))
            for m in args.months
        ]
    else:
        month_starts = [
            dt.date(2026, 4, 1),
            dt.date(2026, 5, 1),
            dt.date(2026, 6, 1),
        ]

    init_db()
    db = SessionLocal()
    try:
        anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
        if anchor_ts is None:
            raise SystemExit("No herd inventory import found.")
        anchor_date = anchor_ts.date()
        print(f"Anchor import: {anchor_ts}")

        for month_start in month_starts:
            _summarize_month(
                db,
                farm=args.farm,
                month_start=month_start,
                fiscal_year=args.fiscal_year,
                anchor_ts=anchor_ts,
                anchor_date=anchor_date,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
