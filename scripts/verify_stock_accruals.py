"""Quick verification for stock accruals."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.models import STOCK_GROUP_BEEF, CowEvent, HerdBirth, StockPurchaseAnimal
from app.services.stock_accruals import build_stock_accruals_report
from app.services.stock_purchase_derivation import rebuild_stock_purchases


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        stats = rebuild_stock_purchases(db)
        db.commit()
        print("Purchases rebuilt:", stats["rows_imported"], stats["stock_group_counts"])

        fresh_dupes = db.scalar(
            select(func.count())
            .select_from(
                select(
                    CowEvent.farm,
                    func.coalesce(CowEvent.etag, CowEvent.cow_id),
                    CowEvent.event_date,
                    CowEvent.lact,
                )
                .where(CowEvent.event == "FRESH")
                .where(CowEvent.event_date.isnot(None))
                .group_by(
                    CowEvent.farm,
                    func.coalesce(CowEvent.etag, CowEvent.cow_id),
                    CowEvent.event_date,
                    CowEvent.lact,
                )
                .having(func.count() > 1)
                .subquery()
            )
        )
        assert fresh_dupes == 0, f"duplicate FRESH event groups: {fresh_dupes}"

        dupes = db.scalar(
            select(func.count())
            .select_from(
                select(StockPurchaseAnimal.farm, StockPurchaseAnimal.etag)
                .group_by(StockPurchaseAnimal.farm, StockPurchaseAnimal.etag)
                .having(func.count() > 1)
                .subquery()
            )
        )
        assert dupes == 0, f"duplicate farm+etag rows: {dupes}"

        birth_dupes = db.scalar(
            select(func.count())
            .select_from(
                select(HerdBirth.farm, HerdBirth.etag)
                .where(HerdBirth.etag.isnot(None))
                .group_by(HerdBirth.farm, HerdBirth.etag)
                .having(func.count() > 1)
                .subquery()
            )
        )
        assert birth_dupes == 0, f"duplicate herd_births farm+etag rows: {birth_dupes}"

        beef_count = db.scalar(
            select(func.count()).where(StockPurchaseAnimal.stock_group == STOCK_GROUP_BEEF)
        )
        assert beef_count and beef_count > 0, "expected some beef purchases"

        report = build_stock_accruals_report(
            db,
            farms=["CM"],
            stock_group="cows",
            month_from=dt.date(2024, 4, 1),
            month_to=dt.date(2024, 4, 30),
        )
        rows = report["rows"]
        assert rows, "expected rows"
        apr = rows[0]
        assert apr["opening"] == 2504, f"opening {apr['opening']}"
        expected = (
            apr["opening"]
            - apr["sales_total"]
            - apr["deaths"]
            + apr["births"]
            + apr["calvings"]
            + apr["purchases"]
        )
        assert apr["closing"] == expected, f"closing mismatch {apr['closing']} vs {expected}"
        print("CM Apr-24 cows opening:", apr["opening"], "closing:", apr["closing"])

        gad = build_stock_accruals_report(
            db,
            farms=["GAD"],
            stock_group="cows",
            month_from=dt.date(2024, 12, 1),
            month_to=dt.date(2024, 12, 31),
        )
        gad_dec = gad["rows"][0]
        assert gad_dec["opening"] == 851
        assert gad_dec["purchases"] == 0, (
            f"GAD Dec-24 cow purchases should be 0 after heifer reclass: {gad_dec['purchases']}"
        )
        print("GAD Dec-24 cows opening:", gad_dec["opening"], "purchases:", gad_dec["purchases"])

        gad_ys = build_stock_accruals_report(
            db,
            farms=["GAD"],
            stock_group="youngstock",
            month_from=dt.date(2024, 12, 1),
            month_to=dt.date(2024, 12, 31),
        )
        gad_ys_dec = gad_ys["rows"][0]
        assert gad_ys_dec["purchases"] >= 10, (
            f"expected purchased heifers in GAD Dec-24 youngstock: {gad_ys_dec['purchases']}"
        )
        print(
            "GAD Dec-24 youngstock purchases:",
            gad_ys_dec["purchases"],
            "calvings:",
            gad_ys_dec["calvings"],
        )

        both = build_stock_accruals_report(
            db,
            farms=["CM", "GAD"],
            stock_group="cows",
            month_from=dt.date(2024, 12, 1),
            month_to=dt.date(2024, 12, 31),
        )
        assert both["rows"], "expected combined farm rows"
        print("Combined farms rows:", len(both["rows"]))

        ys = build_stock_accruals_report(
            db,
            farms=["CM"],
            stock_group="youngstock",
            month_from=dt.date(2024, 12, 1),
            month_to=dt.date(2024, 12, 31),
        )
        if ys["rows"]:
            assert ys["rows"][0]["calvings"] <= 0
            print("Youngstock calvings (negative movement):", ys["rows"][0]["calvings"])
        print("ok")
    finally:
        db.close()


if __name__ == "__main__":
    main()
