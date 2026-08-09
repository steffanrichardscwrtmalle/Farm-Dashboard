"""One-off: seed feed_contracts from app/seed_data/feedcontracts.xlsx.

Uses DATABASE_URL (Render Shell already has production). Locally against
production:

  $env:DATABASE_URL="postgresql+psycopg://…"
  py scripts/seed_feed_contracts.py
  py scripts/seed_feed_contracts.py --force --yes   # replace all rows

Default behaviour matches startup: seed only when the table is empty.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.models import FeedContract
from app.services.feed_contracts import (
    SEED_SOURCE_FILE,
    get_feed_contract_options,
    parse_feedcontracts_xlsx,
    seed_feed_contracts_if_empty,
)

_SEED_PATH = _PROJECT_ROOT / "app" / "seed_data" / "feedcontracts.xlsx"


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed feed contracts from xlsx.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing feed_contracts rows then re-seed",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not _SEED_PATH.is_file():
        print(f"ERROR: seed file missing: {_SEED_PATH}", file=sys.stderr)
        sys.exit(1)

    parsed = parse_feedcontracts_xlsx(_SEED_PATH.read_bytes())
    print(f"Seed file: {_SEED_PATH}")
    print(f"Parsed rows: {len(parsed)}")

    if args.dry_run:
        print("Dry run only — no database changes.")
        return

    if args.force and not args.yes:
        print("ERROR: Pass --yes with --force to confirm overwrite.")
        sys.exit(1)

    init_db()
    db = SessionLocal()
    try:
        existing = db.scalar(select(func.count()).select_from(FeedContract)) or 0
        print(f"Existing feed_contracts: {existing}")

        if args.force:
            db.execute(delete(FeedContract))
            db.commit()
            print("Cleared existing rows.")
            existing = 0

        if existing > 0:
            result = seed_feed_contracts_if_empty(db)
            print(result)
            print("Table already had rows — left unchanged (use --force --yes to replace).")
            return

        db.bulk_insert_mappings(
            FeedContract,
            [{**row, "source_file": SEED_SOURCE_FILE} for row in parsed],
        )
        db.commit()
        get_feed_contract_options(db)
        print(f"Seeded {len(parsed)} feed contracts.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
