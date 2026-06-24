"""Compare stock valuation headcounts to Stock Accruals closing figures."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.stock_valuations import compare_valuations_to_accruals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fiscal-year", type=int, default=2027)
    parser.add_argument("--farm", action="append", dest="farms", help="CM and/or GAD")
    parser.add_argument("--only-mismatches", action="store_true")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        result = compare_valuations_to_accruals(
            db,
            farms=args.farms,
            fiscal_year=args.fiscal_year,
        )
        anchor = result.get("anchor_date")
        print(f"Anchor: {anchor}  Fiscal year: {result['fiscal_year']}")
        print(f"Mismatches: {result['mismatches']} / {len(result['rows'])} comparisons")
        print()

        rows = result["rows"]
        if args.only_mismatches:
            rows = [row for row in rows if not row["matched"]]

        for row in rows:
            status = "OK" if row["matched"] else "MISMATCH"
            jv_note = ""
            if row["jv_beef_adjustment"]:
                jv_note = f" (accruals {row['accruals_closing']} - JV {row['jv_beef_adjustment']})"
            print(
                f"{status:8} {row['month_start']} {row['farm']:3} {row['stock_group']:11} "
                f"val={row['valuation_count']:5} expected={row['expected']:5} "
                f"delta={row['delta']:+5}{jv_note}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
