"""Manual milk collection entry."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection
from app.services.haulier_collections import (
    MANUAL_SOURCE_FILE,
    _build_trend,
    create_manual_collection,
    delete_manual_collection_day,
    get_manual_collection_day,
)
from app.services.haulier_import import _dedupe_month_emails


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_create_manual_collection_writes_loads_and_cows() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    result = create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 12000, "temp_c": 3.4, "sample_id": "101"},
            {"volume_litres": 8000, "temp_c": 3.6, "sample_id": "102"},
            {"volume_litres": None, "temp_c": None},
        ],
        cows_in_milk=412,
    )
    assert result["loads_created"] == 2
    rows = db.scalars(
        select(MilkCollection).where(MilkCollection.farm == "GAD").order_by(
            MilkCollection.arrival_time
        )
    ).all()
    assert len(rows) == 2
    assert rows[0].volume_litres == 12000
    assert rows[0].temp_c == 3.4
    assert rows[0].sample_id == "101"
    assert rows[0].cows_in_milk == 412
    assert rows[0].source_file == MANUAL_SOURCE_FILE
    assert rows[1].volume_litres == 8000
    assert rows[1].sample_id == "102"
    assert rows[1].cows_in_milk == 412


def test_manual_collection_replaces_same_day_manuals() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[{"volume_litres": 1000, "temp_c": 4.0, "sample_id": "010"}],
        cows_in_milk=400,
    )
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 2000, "temp_c": 3.5, "sample_id": "011"},
            {"volume_litres": 1500, "temp_c": 3.7, "sample_id": "012"},
        ],
        cows_in_milk=405,
    )
    rows = db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")).all()
    assert len(rows) == 2
    assert {r.volume_litres for r in rows} == {2000, 1500}
    assert {r.sample_id for r in rows} == {"011", "012"}
    assert all(r.cows_in_milk == 405 for r in rows)


def test_get_and_edit_manual_collection_day() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 5000, "temp_c": 3.2, "sample_id": "201"},
            {"volume_litres": 6000, "temp_c": 3.3, "sample_id": "202"},
        ],
        cows_in_milk=400,
    )
    day_data = get_manual_collection_day(db, farm="GAD", collection_date=day)
    assert day_data["farm"] == "GAD"
    assert day_data["collection_date"] == "2026-08-09"
    assert day_data["cows_in_milk"] == 400
    assert day_data["loads"][0]["sample_id"] == "201"
    assert day_data["loads"][1]["volume_litres"] == 6000

    next_day = dt.date(2026, 8, 10)
    create_manual_collection(
        db,
        collection_date=next_day,
        farm="CM",
        loads=[{"volume_litres": 7000, "temp_c": 3.1, "sample_id": "201"}],
        cows_in_milk=398,
        replace_farm="GAD",
        replace_date=day,
    )
    old_rows = db.scalars(
        select(MilkCollection).where(
            MilkCollection.farm == "GAD",
            MilkCollection.collection_date == day,
        )
    ).all()
    assert old_rows == []
    new_rows = db.scalars(
        select(MilkCollection).where(MilkCollection.farm == "CM")
    ).all()
    assert len(new_rows) == 1
    assert new_rows[0].sample_id == "201"
    assert new_rows[0].volume_litres == 7000


def test_trend_includes_litres_per_cow() -> None:
    trend = _build_trend(
        [
            {
                "farm": "GAD",
                "collection_date": "2026-08-09",
                "volume_litres": 10000,
                "temp_c": 3.2,
                "cows_in_milk": 500,
            },
            {
                "farm": "GAD",
                "collection_date": "2026-08-09",
                "volume_litres": 5000,
                "temp_c": 3.4,
                "cows_in_milk": 500,
            },
            {
                "farm": "GAD",
                "collection_date": "2026-08-10",
                "volume_litres": 12000,
                "temp_c": 3.1,
                "cows_in_milk": None,
            },
        ]
    )
    by_date = {p["date"]: p for p in trend["GAD"]}
    assert by_date["2026-08-09"]["volume_litres"] == 15000
    assert by_date["2026-08-09"]["litres_per_cow"] == 30.0
    assert by_date["2026-08-10"]["litres_per_cow"] is None


def test_delete_manual_collection_day() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 1000, "temp_c": 3.0, "sample_id": "301"},
            {"volume_litres": 2000, "temp_c": 3.1, "sample_id": "302"},
        ],
        cows_in_milk=400,
    )
    result = delete_manual_collection_day(db, farm="GAD", collection_date=day)
    assert result["loads_deleted"] == 2
    rows = db.scalars(
        select(MilkCollection).where(
            MilkCollection.farm == "GAD",
            MilkCollection.collection_date == day,
        )
    ).all()
    assert rows == []


def test_manual_sample_clash_with_imported_row() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="050",
            volume_litres=9000,
            temp_c=3.2,
            source_file="email.xlsx",
        )
    )
    db.commit()
    try:
        create_manual_collection(
            db,
            collection_date=day,
            farm="GAD",
            loads=[{"volume_litres": 1000, "temp_c": 3.0, "sample_id": "050"}],
            cows_in_milk=400,
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_month_email_dedupe_keeps_manual_rows() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[{"volume_litres": 1111, "temp_c": 3.1}],
        cows_in_milk=410,
    )
    older = dt.datetime(2026, 8, 1, 8, 0, 0)
    newer = dt.datetime(2026, 8, 8, 8, 0, 0)
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="026",
            volume_litres=9000,
            temp_c=3.2,
            source_file="old.xlsx",
            source_message_id="msg-old",
            source_received=older,
        )
    )
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="027",
            volume_litres=9500,
            temp_c=3.3,
            source_file="new.xlsx",
            source_message_id="msg-new",
            source_received=newer,
        )
    )
    db.commit()

    removed = _dedupe_month_emails(db, {"GAD"})
    db.commit()
    assert removed == 1
    rows = db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")).all()
    sources = {r.source_file for r in rows}
    assert MANUAL_SOURCE_FILE in sources
    assert "new.xlsx" in sources
    assert "old.xlsx" not in sources
    manual = next(r for r in rows if r.source_file == MANUAL_SOURCE_FILE)
    assert manual.volume_litres == 1111
