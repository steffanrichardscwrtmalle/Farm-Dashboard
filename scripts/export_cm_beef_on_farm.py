"""Export CM beef animals on farm at inventory anchor (valuation logic)."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.models import HERD_FARM_OPTIONS, HerdInventory, STOCK_GROUP_BEEF
from app.services.stock_valuations import (
    _build_profiles,
    _on_farm_keys,
    _resolve_state_at,
    build_stock_valuations_report,
)

_FARM = "CM"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        anchor_ts = db.scalar(select(func.max(HerdInventory.import_timestamp)))
        if anchor_ts is None:
            raise SystemExit("No herd inventory import found.")

        anchor_date, profiles, inventory_keys, exit_keys, entry_keys, jv_keys = (
            _build_profiles(
                db,
                selected_farms=list(HERD_FARM_OPTIONS),
                anchor_ts=anchor_ts,
            )
        )
        close_date = anchor_date
        keys = _on_farm_keys(
            close_date,
            anchor_date,
            inventory_keys,
            exit_keys,
            entry_keys,
            jv_keys,
            profiles,
        )

        beef_rows: list[dict[str, object]] = []
        for key in sorted(keys):
            if key[0] != _FARM:
                continue
            profile = profiles[key]
            state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
            if state is None or state["stock_group"] != STOCK_GROUP_BEEF:
                continue
            beef_rows.append(
                {
                    "etag": profile.etag or "",
                    "cow_id": profile.cow_id or "",
                    "lact": state["lact"],
                    "category": state["category"],
                    "aged_days": state["aged_days"],
                    "value": state["value"],
                    "bdat": profile.bdat,
                    "inventory_sbrd": profile.inventory_sbrd,
                    "in_anchor_inventory": profile.in_anchor_inventory,
                }
            )

        fiscal_year = anchor_date.year + 1 if anchor_date.month >= 4 else anchor_date.year
        report = build_stock_valuations_report(db, farms=[_FARM], fiscal_year=fiscal_year)
        months = report.get("months", [])
        latest = months[-1] if months else None
        val_beef_count = None
        if latest:
            val_beef_count = latest["totals"][_FARM]["categories"]["Beef"]["count"]

        out_dir = _PROJECT_ROOT / "exports" / "cm_beef_on_farm"
        out_dir.mkdir(parents=True, exist_ok=True)
        etag_path = out_dir / "beef_etags.txt"
        detail_path = out_dir / "beef_detail.csv"

        etags = [str(row["etag"]) for row in beef_rows if row.get("etag")]
        etag_path.write_text("\n".join(etags) + ("\n" if etags else ""), encoding="utf-8")

        with detail_path.open("w", encoding="utf-8") as handle:
            handle.write(
                "etag,cow_id,lact,category,aged_days,value_gbp,bdat,inventory_sbrd,in_anchor_inventory\n"
            )
            for row in beef_rows:
                handle.write(
                    f"{row['etag']},{row['cow_id']},{row['lact']},{row['category']},"
                    f"{row['aged_days']},{row['value']},{row.get('bdat') or ''},"
                    f"{row.get('inventory_sbrd') or ''},{row['in_anchor_inventory']}\n"
                )

        print(f"Anchor date: {anchor_date}")
        print(f"Close date: {close_date}")
        print(f"CM beef on-farm (valuation logic): {len(beef_rows)}")
        if latest:
            print(
                f"CM beef in latest valuation month ({latest['month_start']}): "
                f"{val_beef_count}"
            )
        print(f"Written: {etag_path}")
        print(f"Written: {detail_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
