"""Compare DC305 fresh-heifer ETAG list to app GAD FRESH lact=1."""
import datetime as dt
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import CowEvent

DC305_FILE = Path(__file__).resolve().parent / "_dc305_etags.txt"

ABORT = {
    "UK752261310353",
    "UK752261610468",
    "UK752261210499",
    "UK752261110645",
    "UK752261111065",
    "UK752261711092",
    "UK752261511377",
    "UK752261411432",
}


def load_dc305() -> set[str]:
    text = DC305_FILE.read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def main() -> None:
    dc305_set = load_dc305()
    print(f"DC305 list: {len(dc305_set)} unique ETAGs")

    init_db()
    db = SessionLocal()
    try:
        rows = db.execute(
            select(CowEvent.etag, CowEvent.event_date).where(
                CowEvent.farm == "GAD",
                CowEvent.event == "FRESH",
                CowEvent.lact == 1,
                CowEvent.etag.isnot(None),
            )
        ).all()

        app_by_etag: dict[str, dt.date] = {}
        for etag, event_date in rows:
            e = str(etag).strip()
            d = event_date.date() if hasattr(event_date, "date") else event_date
            if e not in app_by_etag or d < app_by_etag[e]:
                app_by_etag[e] = d

        app_set = set(app_by_etag)
        print(f"App GAD FRESH lact1 unique ETAGs: {len(app_set)}")

        only_app = sorted(app_set - dc305_set)
        only_dc305 = sorted(dc305_set - app_set)
        both = app_set & dc305_set
        print(f"In both: {len(both)}")
        print(f"Only in app: {len(only_app)}")
        print(f"Only in DC305: {len(only_dc305)}")

        # Date ranges
        dc305_dates = [app_by_etag[e] for e in both]
        only_app_dates = [app_by_etag[e] for e in only_app]
        print(f"\nDC305 animals calving date range: {min(dc305_dates)} .. {max(dc305_dates)}")
        if only_app_dates:
            print(f"App-only animals calving date range: {min(only_app_dates)} .. {max(only_app_dates)}")

        # Count by month for both groups
        def month_counts(dates):
            c = Counter((d.year, d.month) for d in dates)
            return sorted(c.items())

        print("\n--- Monthly calvings: DC305 list animals (in app) ---")
        for ym, n in month_counts(dc305_dates)[-12:]:
            print(f"  {ym[0]}-{ym[1]:02d}: {n}")

        print("\n--- Monthly calvings: app-only (not in DC305) ---")
        oac = month_counts(only_app_dates)
        for ym, n in oac[:12]:
            print(f"  {ym[0]}-{ym[1]:02d}: {n}")
        print(f"  ... total months: {len(oac)}")

        # Try common cutoffs
        cutoffs = [
            dt.date(2024, 12, 1),
            dt.date(2025, 1, 1),
            dt.date(2025, 4, 1),
            dt.date(2024, 4, 1),
        ]
        print("\n--- App FRESH lact1 counts by cutoff ---")
        for cutoff in cutoffs:
            in_range = {e for e, d in app_by_etag.items() if d >= cutoff}
            dc_in = dc305_set & in_range
            print(
                f"  >= {cutoff}: app={len(in_range)}, dc305_in_range={len(dc_in)}, "
                f"app_only={len(in_range - dc305_set)}, dc305_only={len(dc305_set - in_range)}"
            )

        # UK740651 in DC305
        dc740 = sorted(e for e in dc305_set if e.startswith("UK740651"))
        app740_only = sorted(e for e in only_app if e.startswith("UK740651"))
        print(f"\nUK740651 in DC305: {len(dc740)}")
        print(f"UK740651 app-only: {app740_only}")

        # ABORT
        print(f"\nABORT converted in DC305: {sorted(ABORT & dc305_set)}")
        print(f"ABORT converted NOT in DC305: {sorted(ABORT - dc305_set)}")

        # Sample app-only recent (2025+)
        recent_only = sorted(
            [(e, app_by_etag[e]) for e in only_app if app_by_etag[e] >= dt.date(2025, 1, 1)],
            key=lambda x: x[1],
        )
        print(f"\nApp-only with calving >= 2025-01-01: {len(recent_only)}")
        for e, d in recent_only[:20]:
            print(f"  {e} {d}")
        if len(recent_only) > 20:
            print(f"  ... and {len(recent_only) - 20} more")

        # DC305 not in app - detail
        if only_dc305:
            print("\n--- In DC305 but NOT in app ---")
            for e in only_dc305:
                print(f"  {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
