"""Import Xero org + budget mappings from JSON seed into DATABASE_URL.

On Render Shell (production DB already in DATABASE_URL):

  python scripts/import_xero_mappings_seed.py --replace --yes

Locally against production:

  $env:DATABASE_URL="postgresql+psycopg://…"
  py scripts/import_xero_mappings_seed.py --replace --yes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import delete, func, select

from app.db import SessionLocal, init_db
from app.models import (
    FinancialForecastMapping,
    XeroAccountBudgetMapping,
    XeroOrganisation,
)

_DEFAULT_SEED = _PROJECT_ROOT / "seeds" / "xero_mappings_seed.json"


def _heading_key(item_type: str, band: str, group: str, heading: str) -> tuple[str, str, str, str]:
    return (
        (item_type or "").strip(),
        (band or "").strip(),
        (group or "").strip(),
        (heading or "").strip(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Xero mappings seed into DATABASE_URL.")
    parser.add_argument(
        "--seed",
        default=str(_DEFAULT_SEED),
        help=f"Seed JSON path (default: {_DEFAULT_SEED})",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing Xero organisations and budget mappings",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    seed_path = Path(args.seed)
    if not seed_path.is_file():
        print(f"ERROR: seed file not found: {seed_path}", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    orgs = payload.get("organisations") or []
    mappings = payload.get("budget_mappings") or []
    print(f"Seed: {seed_path}")
    print(f"  organisations: {len(orgs)}")
    print(f"  budget_mappings: {len(mappings)}")

    if args.replace and not args.yes and not args.dry_run:
        print("ERROR: Pass --yes with --replace to confirm overwrite.")
        sys.exit(1)

    init_db()
    db = SessionLocal()
    try:
        heading_index = {
            _heading_key(row.item_type, row.band, row.group, row.heading): int(row.id)
            for row in db.scalars(select(FinancialForecastMapping)).all()
        }
        before_orgs = db.scalar(select(func.count()).select_from(XeroOrganisation)) or 0
        before_maps = db.scalar(select(func.count()).select_from(XeroAccountBudgetMapping)) or 0
        print(f"Target before: orgs={before_orgs}, budget_mappings={before_maps}")

        if args.dry_run:
            missing = 0
            for item in mappings:
                key = _heading_key(
                    item.get("item_type", ""),
                    item.get("band", ""),
                    item.get("group", ""),
                    item.get("heading", ""),
                )
                if key not in heading_index:
                    missing += 1
            print(f"Dry run — headings missing on target: {missing}")
            return

        if args.replace:
            db.execute(delete(XeroAccountBudgetMapping))
            db.execute(delete(XeroOrganisation))
            db.commit()

        existing_orgs = {
            row.tenant_id: row for row in db.scalars(select(XeroOrganisation)).all()
        }
        org_ins = org_upd = 0
        for org in orgs:
            row = existing_orgs.get(org["tenant_id"])
            if row is None:
                db.add(
                    XeroOrganisation(
                        tenant_id=org["tenant_id"],
                        tenant_name=org.get("tenant_name") or "",
                        tenant_type=org.get("tenant_type"),
                        dashboard_business=org.get("dashboard_business"),
                        is_active=bool(org.get("is_active", True)),
                    )
                )
                org_ins += 1
            else:
                row.tenant_name = org.get("tenant_name") or row.tenant_name
                row.tenant_type = org.get("tenant_type")
                row.dashboard_business = org.get("dashboard_business")
                row.is_active = bool(org.get("is_active", True))
                org_upd += 1
        db.commit()

        existing_maps = {
            (row.tenant_id, row.account_id): row
            for row in db.scalars(select(XeroAccountBudgetMapping)).all()
        }
        written = skipped = 0
        for item in mappings:
            key = _heading_key(
                item.get("item_type", ""),
                item.get("band", ""),
                item.get("group", ""),
                item.get("heading", ""),
            )
            mapping_id = heading_index.get(key)
            if mapping_id is None:
                print(
                    f"  WARN: skip {item.get('account_code') or item.get('account_id')} "
                    f"— heading not found: {key}"
                )
                skipped += 1
                continue
            pair = (item["tenant_id"], item["account_id"])
            row = existing_maps.get(pair)
            if row is None:
                db.add(
                    XeroAccountBudgetMapping(
                        tenant_id=item["tenant_id"],
                        account_id=item["account_id"],
                        account_code=item.get("account_code"),
                        mapping_id=mapping_id,
                    )
                )
            else:
                row.account_code = item.get("account_code")
                row.mapping_id = mapping_id
            written += 1
        db.commit()

        after_orgs = db.scalar(select(func.count()).select_from(XeroOrganisation)) or 0
        after_maps = db.scalar(select(func.count()).select_from(XeroAccountBudgetMapping)) or 0
        print(f"Organisations: inserted={org_ins}, updated={org_upd}")
        print(f"Budget mappings written={written}, skipped={skipped}")
        print(f"Target after: orgs={after_orgs}, budget_mappings={after_maps}")
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
