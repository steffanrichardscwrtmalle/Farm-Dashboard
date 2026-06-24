"""Export CM valuation classifications at a fiscal month-end."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.models import HERD_FARM_OPTIONS, HerdInventory, STOCK_GROUP_BEEF, STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK
from app.services.stock_valuations import (
    _build_profiles,
    _month_end,
    _on_farm_keys,
    _resolve_state_at,
    build_stock_valuations_report,
)

_FARM = "CM"
_GROUPS = (
    (STOCK_GROUP_COWS, "cows"),
    (STOCK_GROUP_YOUNGSTOCK, "youngstock"),
    (STOCK_GROUP_BEEF, "beef"),
)


def _export_month(*, month_start: dt.date) -> None:
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
        close_date = min(_month_end(month_start), anchor_date)
        keys = _on_farm_keys(
            close_date,
            anchor_date,
            inventory_keys,
            exit_keys,
            entry_keys,
            jv_keys,
            profiles,
        )

        grouped: dict[str, list[dict[str, object]]] = {
            STOCK_GROUP_COWS: [],
            STOCK_GROUP_YOUNGSTOCK: [],
            STOCK_GROUP_BEEF: [],
        }
        for key in sorted(keys):
            if key[0] != _FARM:
                continue
            profile = profiles[key]
            state = _resolve_state_at(profile, close_date, anchor_date=anchor_date)
            if state is None:
                continue
            stock_group = state["stock_group"]
            if stock_group not in grouped:
                continue
            grouped[stock_group].append(
                {
                    "etag": profile.etag or "",
                    "cow_id": profile.cow_id or "",
                    "lact": state["lact"],
                    "category": state["category"],
                    "aged_days": state["aged_days"],
                    "value": state["value"],
                    "bdat": profile.bdat,
                    "inventory_sbrd": profile.inventory_sbrd,
                }
            )

        fiscal_year = month_start.year + 1 if month_start.month >= 4 else month_start.year
        report = build_stock_valuations_report(
            db, farms=[_FARM], fiscal_year=fiscal_year, selected_month=month_start
        )
        totals = {}
        month_row = next(
            (row for row in report.get("months", []) if row.get("month_start") == month_start.isoformat()),
            None,
        )
        if month_row:
            totals = month_row.get("totals", {}).get(_FARM, {})

        label = month_start.strftime("%b%y").lower()
        out_dir = _PROJECT_ROOT / "exports" / f"cm_{label}_{month_start.year}"
        out_dir.mkdir(parents=True, exist_ok=True)

        all_rows: list[dict[str, object]] = []
        for stock_group, name in _GROUPS:
            rows = grouped[stock_group]
            all_rows.extend(rows)
            etags = [str(row["etag"]) for row in rows if row.get("etag")]
            (out_dir / f"{name}_etags.txt").write_text(
                "\n".join(etags) + ("\n" if etags else ""), encoding="utf-8"
            )
            with (out_dir / f"{name}_detail.csv").open("w", encoding="utf-8") as handle:
                handle.write("etag,cow_id,stock_group,lact,category,aged_days,value_gbp,bdat,inventory_sbrd\n")
                for row in rows:
                    handle.write(
                        f"{row['etag']},{row['cow_id']},{name},{row['lact']},{row['category']},"
                        f"{row['aged_days']},{row['value']},{row.get('bdat') or ''},"
                        f"{row.get('inventory_sbrd') or ''}\n"
                    )

        all_etags = sorted({str(row["etag"]) for row in all_rows if row.get("etag")})
        (out_dir / "all_etags.txt").write_text(
            "\n".join(all_etags) + ("\n" if all_etags else ""), encoding="utf-8"
        )

        print(f"Anchor date: {anchor_date}")
        print(f"Month: {month_start.isoformat()}  Close: {close_date}")
        for stock_group, name in _GROUPS:
            report_count = totals.get("dairy_cows" if name == "cows" else "categories", {})
            if name == "cows":
                count = report_count if isinstance(report_count, int) else totals.get("dairy_cows")
            else:
                count = totals.get("categories", {}).get(
                    {"beef": "Beef", "youngstock": "Youngstock"}[name], {}
                ).get("count")
            print(f"  {name}: {len(grouped[stock_group])} exported (report: {count})")
        print(f"  all: {len(all_etags)}")
        print(f"Written to: {out_dir}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default="2026-06", help="YYYY-MM (default: 2026-06)")
    args = parser.parse_args()
    year_s, month_s = args.month.split("-", 1)
    month_start = dt.date(int(year_s), int(month_s), 1)
    _export_month(month_start=month_start)


if __name__ == "__main__":
    main()
