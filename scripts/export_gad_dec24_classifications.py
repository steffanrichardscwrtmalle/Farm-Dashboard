"""Export GAD valuation stock-group classifications at Dec 2024 month-end (mirror logic)."""
from __future__ import annotations

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
)

_FARM = "GAD"
_MONTH_START = dt.date(2024, 12, 1)


def _export_rows(
    *,
    close_date: dt.date,
    anchor_date: dt.date,
    profiles,
    keys: set[tuple[str, str]],
) -> dict[str, list[dict[str, object]]]:
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
                "etag": profile.etag,
                "cow_id": profile.cow_id,
                "lact": state["lact"],
                "bdat": profile.bdat,
                "inventory_lact": profile.inventory_lact,
                "in_anchor_inventory": profile.in_anchor_inventory,
            }
        )
    return grouped


def _write_etag_list(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [str(row["etag"]) for row in rows if row.get("etag")]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_detail_csv(path: Path, rows: list[dict[str, object]], stock_group: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "etag,cow_id,stock_group,lact,bdat,inventory_lact,in_anchor_inventory\n"
    body = "".join(
        f"{row.get('etag','')},{row.get('cow_id','')},{stock_group},{row.get('lact','')},"
        f"{row.get('bdat','')},{row.get('inventory_lact','')},{row.get('in_anchor_inventory','')}\n"
        for row in rows
    )
    path.write_text(header + body, encoding="utf-8")


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
        close_date = min(_month_end(_MONTH_START), anchor_date)
        keys = _on_farm_keys(
            close_date,
            anchor_date,
            inventory_keys,
            exit_keys,
            entry_keys,
            jv_keys,
            profiles,
        )
        grouped = _export_rows(
            close_date=close_date,
            anchor_date=anchor_date,
            profiles=profiles,
            keys=keys,
        )

        out_dir = _PROJECT_ROOT / "exports" / "gad_dec24_2024"
        youngstock = grouped[STOCK_GROUP_YOUNGSTOCK]

        _write_etag_list(out_dir / "youngstock_etags.txt", youngstock)

        print(f"Anchor date: {anchor_date}  Close: {close_date}")
        print(f"Youngstock: {len(youngstock)}")
        print(f"Written to: {out_dir / 'youngstock_etags.txt'}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
