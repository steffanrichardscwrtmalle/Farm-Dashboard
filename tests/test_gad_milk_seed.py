"""Historical GAD milk ticket seed from gadmilk.xlsx."""

from __future__ import annotations

import datetime as dt
import io

from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection
from app.services.gad_milk_seed import parse_gadmilk_xlsx, seed_gad_milk_collections
from app.services.haulier_collections import SEED_GAD_MILK_SOURCE_FILE


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _tiny_workbook_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Milk Tickets"
    ws.append(["Date", "Load 1", "Load2", "Cows In Milk"])
    ws.append([dt.datetime(2025, 1, 1), 10000, 15000, None])
    ws.append([dt.datetime(2025, 1, 2), 11000, 14000, 700])
    ws.append([dt.datetime(2025, 1, 3), None, None, None])  # placeholder
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_gadmilk_skips_empty_days() -> None:
    days = parse_gadmilk_xlsx(_tiny_workbook_bytes())
    assert len(days) == 2
    assert days[0]["collection_date"] == dt.date(2025, 1, 1)
    assert days[0]["loads"] == [10000, 15000]
    assert days[0]["cows_in_milk"] is None
    assert days[1]["cows_in_milk"] == 700


def test_seed_inserts_missing_days_only(tmp_path) -> None:
    path = tmp_path / "gadmilk.xlsx"
    path.write_bytes(_tiny_workbook_bytes())
    db = _session()

    first = seed_gad_milk_collections(db, path=path)
    assert first["days_inserted"] == 2
    assert first["loads_inserted"] == 4

    rows = db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")).all()
    assert len(rows) == 4
    assert all(r.source_file == SEED_GAD_MILK_SOURCE_FILE for r in rows)
    day2 = [r for r in rows if r.collection_date == dt.date(2025, 1, 2)]
    assert {r.volume_litres for r in day2} == {11000, 14000}
    assert all(r.cows_in_milk == 700 for r in day2)

    # Existing day is left alone; re-seed is a no-op for those dates.
    for row in rows:
        if row.collection_date == dt.date(2025, 1, 1):
            row.volume_litres = 999
    db.commit()

    second = seed_gad_milk_collections(db, path=path)
    assert second["days_inserted"] == 0
    assert second["skipped_existing"] == 2
    kept = db.scalars(
        select(MilkCollection).where(
            MilkCollection.farm == "GAD",
            MilkCollection.collection_date == dt.date(2025, 1, 1),
        )
    ).all()
    assert {r.volume_litres for r in kept} == {999}


def test_repo_seed_file_parses() -> None:
    from pathlib import Path

    seed_path = (
        Path(__file__).resolve().parents[1] / "app" / "seed_data" / "gadmilk.xlsx"
    )
    assert seed_path.is_file()
    days = parse_gadmilk_xlsx(seed_path.read_bytes())
    assert len(days) >= 600
    assert days[0]["collection_date"] == dt.date(2024, 12, 2)
    assert days[0]["loads"] == [12025, 15969]
