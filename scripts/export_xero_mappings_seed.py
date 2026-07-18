"""Export local Xero org + budget mappings to a JSON seed file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import FinancialForecastMapping, XeroAccountBudgetMapping, XeroOrganisation

_DEFAULT_OUT = _PROJECT_ROOT / "seeds" / "xero_mappings_seed.json"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_OUT
    init_db()
    db = SessionLocal()
    try:
        headings = {
            int(row.id): {
                "item_type": row.item_type,
                "band": row.band,
                "group": row.group,
                "heading": row.heading,
            }
            for row in db.scalars(select(FinancialForecastMapping)).all()
        }
        orgs = [
            {
                "tenant_id": row.tenant_id,
                "tenant_name": row.tenant_name or "",
                "tenant_type": row.tenant_type,
                "dashboard_business": row.dashboard_business,
                "is_active": bool(row.is_active),
            }
            for row in db.scalars(select(XeroOrganisation)).all()
        ]
        mappings = []
        skipped = 0
        for row in db.scalars(select(XeroAccountBudgetMapping)).all():
            heading = headings.get(int(row.mapping_id))
            if heading is None:
                skipped += 1
                continue
            mappings.append(
                {
                    "tenant_id": row.tenant_id,
                    "account_id": row.account_id,
                    "account_code": row.account_code,
                    **heading,
                }
            )
        payload = {
            "version": 1,
            "organisations": orgs,
            "budget_mappings": mappings,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
        print(f"  organisations: {len(orgs)}")
        print(f"  budget_mappings: {len(mappings)}")
        if skipped:
            print(f"  skipped (unknown mapping_id): {skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
