"""Export GAD beef calf births for a calendar month (stock accruals rules)."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import and_, func, or_, select

from app.db import SessionLocal, init_db
from app.models import HerdBirth
from app.services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_BEEF


def _beef_birth_filter():
    return or_(
        HerdBirth.category == CATEGORY_BEEF,
        and_(
            HerdBirth.category.is_(None),
            or_(
                func.upper(func.coalesce(HerdBirth.gndr, "")) != "F",
                HerdBirth.cbrd.is_(None),
                HerdBirth.cbrd >= BEEF_CBREED_MIN,
            ),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--farm", default="GAD")
    parser.add_argument("--month", default="2024-12", help="YYYY-MM")
    args = parser.parse_args()

    year_s, month_s = args.month.split("-", 1)
    month_start = dt.date(int(year_s), int(month_s), 1)
    if month_start.month == 12:
        month_end = dt.date(month_start.year, 12, 31)
    else:
        month_end = dt.date(month_start.year, month_start.month + 1, 1) - dt.timedelta(days=1)

    init_db()
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(HerdBirth)
            .where(HerdBirth.farm == args.farm)
            .where(HerdBirth.bdat >= month_start)
            .where(HerdBirth.bdat <= month_end)
            .where(_beef_birth_filter())
            .order_by(HerdBirth.bdat, HerdBirth.etag)
        ).all()

        label = month_start.strftime("%b%y").lower()
        out_dir = _PROJECT_ROOT / "exports" / f"{args.farm.lower()}_{label}_{month_start.year}"
        out_dir.mkdir(parents=True, exist_ok=True)
        etag_path = out_dir / "beef_births_etags.txt"
        detail_path = out_dir / "beef_births_detail.csv"

        etags = [row.etag for row in rows if row.etag]
        etag_path.write_text("\n".join(etags) + ("\n" if etags else ""), encoding="utf-8")

        with detail_path.open("w", encoding="utf-8") as handle:
            handle.write("bdat,etag,cow_id,category,cbrd,gndr\n")
            for row in rows:
                handle.write(
                    f"{row.bdat},{row.etag or ''},{row.cow_id or ''},"
                    f"{row.category or ''},{row.cbrd or ''},{row.gndr or ''}\n"
                )

        print(f"{args.farm} beef calves born {month_start.strftime('%b-%Y')}: {len(rows)}")
        for row in rows:
            print(
                f"  {row.bdat}  {row.etag or '(no etag)'}  "
                f"cow_id={row.cow_id}  cat={row.category}  cbrd={row.cbrd}  gndr={row.gndr}"
            )
        print(f"Written: {etag_path}")
        print(f"Written: {detail_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
