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


def test_create_manual_collection_treats_zero_volume_as_blank() -> None:
    db = _session()
    day = dt.date(2026, 8, 9)
    result = create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 0, "temp_c": None, "sample_id": ""},
            {"volume_litres": 0, "temp_c": 3.5, "sample_id": "401"},
            {"volume_litres": 9000, "temp_c": 3.2, "sample_id": "402"},
        ],
        cows_in_milk=400,
    )
    # Zero volume is never a load, even with temp/sample.
    assert result["loads_created"] == 1
    rows = db.scalars(
        select(MilkCollection)
        .where(MilkCollection.farm == "GAD")
        .order_by(MilkCollection.arrival_time)
    ).all()
    assert len(rows) == 1
    assert rows[0].volume_litres == 9000
    assert rows[0].sample_id == "402"

    try:
        create_manual_collection(
            db,
            collection_date=dt.date(2026, 8, 10),
            farm="GAD",
            loads=[{"volume_litres": 0}, {"volume_litres": 0.0, "sample_id": "x"}],
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "volume" in str(exc).lower()


def test_zero_volume_rows_excluded_from_trend_totals() -> None:
    from app.services.haulier_collections import _summary

    rows = [
        {
            "farm": "GAD",
            "collection_date": "2026-08-09",
            "volume_litres": 0,
            "temp_c": 3.0,
            "matched": False,
            "butterfat_pct": None,
            "protein_pct": None,
            "cows_in_milk": 400,
        },
        {
            "farm": "GAD",
            "collection_date": "2026-08-09",
            "volume_litres": 10000,
            "temp_c": 3.2,
            "matched": False,
            "butterfat_pct": None,
            "protein_pct": None,
            "cows_in_milk": 400,
        },
    ]
    summary = _summary(rows)
    assert summary["total_volume"] == 10000
    assert summary["avg_daily_volume"] == 10000
    points = _build_trend(rows)["GAD"]
    assert points[0]["volume_litres"] == 10000


def test_pair_samples_to_loads_by_volume() -> None:
    from app.services.haulier_collections import _pair_samples_to_loads_by_volume

    # Lowest sample number → highest volume; result stays in load order.
    assert _pair_samples_to_loads_by_volume([8000, 12000], ["1002", "1001"]) == [
        "1002",
        "1001",
    ]
    assert _pair_samples_to_loads_by_volume([15000, 8000, 12000], ["12", "10", "11"]) == [
        "10",
        "12",
        "11",
    ]
    # Leading zeros still sort numerically.
    assert _pair_samples_to_loads_by_volume([9000, 11000], ["091", "090"]) == [
        "091",
        "090",
    ]
    assert _pair_samples_to_loads_by_volume([8000, 7000], ["2002"]) is None
    assert _pair_samples_to_loads_by_volume([14000], ["7788"]) == ["7788"]


def test_backfill_gad_sample_ids_from_nml_single_load_days() -> None:
    from app.models import NmlMilkResult
    from app.services.haulier_collections import backfill_gad_sample_ids_from_nml

    db = _session()
    day = dt.date(2026, 8, 22)
    day_mismatch = dt.date(2026, 8, 23)
    day_multi = dt.date(2026, 8, 24)
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id=None,
            volume_litres=15000,
            source_file="seed:gadmilk.xlsx",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="4455",
        )
    )
    # Two loads and one NML sample must not be guessed
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day_mismatch,
            sample_id=None,
            volume_litres=8000,
            arrival_time=dt.time(1, 0),
            source_file="seed:gadmilk.xlsx",
        )
    )
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day_mismatch,
            sample_id=None,
            volume_litres=7000,
            arrival_time=dt.time(2, 0),
            source_file="seed:gadmilk.xlsx",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day_mismatch,
            sample_id="4466",
        )
    )
    # Two loads and two samples: lowest sample → highest volume
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day_multi,
            sample_id=None,
            volume_litres=8000,
            arrival_time=dt.time(1, 0),
            source_file="seed:gadmilk.xlsx",
        )
    )
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day_multi,
            sample_id=None,
            volume_litres=12000,
            arrival_time=dt.time(2, 0),
            source_file="seed:gadmilk.xlsx",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day_multi,
            sample_id="1002",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day_multi,
            sample_id="1001",
        )
    )
    db.commit()

    updated = backfill_gad_sample_ids_from_nml(db)
    assert updated == 3
    single = db.scalars(
        select(MilkCollection).where(MilkCollection.collection_date == day)
    ).one()
    assert single.sample_id == "4455"
    mismatch = db.scalars(
        select(MilkCollection).where(MilkCollection.collection_date == day_mismatch)
    ).all()
    assert all(not (r.sample_id or "").strip() for r in mismatch)
    multi = {
        r.volume_litres: r.sample_id
        for r in db.scalars(
            select(MilkCollection).where(MilkCollection.collection_date == day_multi)
        ).all()
    }
    assert multi[12000] == "1001"
    assert multi[8000] == "1002"


def test_backfill_gad_repairs_seed_samples_by_volume() -> None:
    from app.models import NmlMilkResult
    from app.services.haulier_collections import (
        SEED_GAD_MILK_SOURCE_FILE,
        backfill_gad_sample_ids_from_nml,
    )

    db = _session()
    day = dt.date(2026, 8, 25)
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="1001",
            volume_litres=8000,
            arrival_time=dt.time(1, 0),
            source_file=SEED_GAD_MILK_SOURCE_FILE,
        )
    )
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="1002",
            volume_litres=12000,
            arrival_time=dt.time(2, 0),
            source_file=SEED_GAD_MILK_SOURCE_FILE,
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1001",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1002",
        )
    )
    db.commit()

    updated = backfill_gad_sample_ids_from_nml(db)
    assert updated == 2
    by_volume = {
        r.volume_litres: r.sample_id
        for r in db.scalars(
            select(MilkCollection).where(MilkCollection.collection_date == day)
        ).all()
    }
    assert by_volume[12000] == "1001"
    assert by_volume[8000] == "1002"


def test_backfill_gad_leaves_haulier_sample_ids_alone() -> None:
    from app.models import NmlMilkResult
    from app.services.haulier_collections import backfill_gad_sample_ids_from_nml

    db = _session()
    day = dt.date(2026, 8, 26)
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="1001",
            volume_litres=8000,
            arrival_time=dt.time(6, 0),
            source_file="haulier.xlsx",
        )
    )
    db.add(
        MilkCollection(
            farm="GAD",
            collection_date=day,
            sample_id="1002",
            volume_litres=12000,
            arrival_time=dt.time(14, 0),
            source_file="haulier.xlsx",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1001",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1002",
        )
    )
    db.commit()

    assert backfill_gad_sample_ids_from_nml(db) == 0
    by_volume = {
        r.volume_litres: r.sample_id
        for r in db.scalars(
            select(MilkCollection).where(MilkCollection.collection_date == day)
        ).all()
    }
    assert by_volume[8000] == "1001"
    assert by_volume[12000] == "1002"


def test_list_collections_includes_unmatched_nml_and_quality_summary() -> None:
    from app.models import NmlMilkResult
    from app.services.haulier_collections import list_collections

    db = _session()
    day = dt.date(2026, 8, 21)
    db.add(
        MilkCollection(
            farm="CM",
            collection_date=day,
            sample_id="601",
            volume_litres=20000,
            arrival_time=dt.time(6, 0),
            source_file="haulier.xlsx",
        )
    )
    db.add(
        NmlMilkResult(
            farm="CM",
            producer_ref="389000184",
            sample_date=day,
            sample_id="601",
            butterfat_pct=4.1,
            protein_pct=3.3,
            scc=120,
            bactoscan=25,
            antibiotic_pass=True,
        )
    )
    db.add(
        NmlMilkResult(
            farm="CM",
            producer_ref="389000184",
            sample_date=day,
            sample_id="999",
            butterfat_pct=4.5,
            protein_pct=3.5,
            scc=200,
            bactoscan=55,
            antibiotic_pass=False,
            milk_buyer="Dairy Partners",
            fpd=520,
        )
    )
    db.commit()

    result = list_collections(db, farms=["CM"], date_from=day, date_to=day)
    assert result["total"] == 1
    assert result["rows"][0]["matched"] is True
    assert result["rows"][0]["scc"] == 120
    assert len(result["unmatched_nml"]) == 1
    assert result["unmatched_nml"][0]["sample_id"] == "999"
    assert result["summary"]["antibiotic_fails"] == 1
    assert result["summary"]["unmatched_nml_count"] == 1
    assert result["summary"]["avg_scc"] == 160.0  # (120+200)/2
    # Unmatched NML still appears in quality trend for that day
    assert any(p.get("scc") is not None for p in result["trend"]["CM"])


def test_list_collections_deletes_blank_volume_rows() -> None:
    from app.services.haulier_collections import list_collections

    db = _session()
    day = dt.date(2026, 8, 20)
    db.add(
        MilkCollection(
            farm="CM",
            collection_date=day,
            sample_id="501",
            volume_litres=None,
            arrival_time=dt.time(6, 0),
            source_file="haulier.xlsx",
        )
    )
    db.add(
        MilkCollection(
            farm="CM",
            collection_date=day,
            sample_id="502",
            volume_litres=0,
            arrival_time=dt.time(7, 0),
            source_file="haulier.xlsx",
        )
    )
    db.add(
        MilkCollection(
            farm="CM",
            collection_date=day,
            sample_id="503",
            volume_litres=25000,
            arrival_time=dt.time(8, 0),
            source_file="haulier.xlsx",
        )
    )
    db.commit()

    result = list_collections(db, farms=["CM"], date_from=day, date_to=day)
    assert result["total"] == 1
    assert result["rows"][0]["sample_id"] == "503"
    assert result["rows"][0]["volume_litres"] == 25000
    remaining = db.scalars(select(MilkCollection).where(MilkCollection.farm == "CM")).all()
    assert len(remaining) == 1
    assert remaining[0].sample_id == "503"


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


def test_trend_bactoscan_is_volume_weighted() -> None:
    trend = _build_trend(
        [
            {
                "farm": "CM",
                "collection_date": "2026-08-09",
                "volume_litres": 10000,
                "bactoscan": 20,
                "temp_c": None,
                "cows_in_milk": 500,
            },
            {
                "farm": "CM",
                "collection_date": "2026-08-09",
                "volume_litres": 30000,
                "bactoscan": 40,
                "temp_c": None,
                "cows_in_milk": 500,
            },
        ]
    )
    # (20*10000 + 40*30000) / 40000 = 35
    assert trend["CM"][0]["bactoscan"] == 35.0
    assert trend["CM"][0]["volume_litres"] == 40000


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


def test_gad_single_load_autofills_sample_from_nml() -> None:
    from app.models import NmlMilkResult
    from app.services.haulier_collections import suggest_sample_for_manual_day

    db = _session()
    day = dt.date(2026, 8, 12)
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="7788",
            butterfat_pct=4.1,
        )
    )
    db.commit()

    suggestion = suggest_sample_for_manual_day(
        db, farm="GAD", collection_date=day, load_count=1
    )
    assert suggestion["sample_id"] == "7788"

    cm_suggestion = suggest_sample_for_manual_day(
        db, farm="CM", collection_date=day, load_count=1
    )
    assert cm_suggestion["sample_id"] is None
    assert cm_suggestion["reason"] == "gad_only"

    result = create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[{"volume_litres": 14000, "temp_c": 3.4, "sample_id": ""}],
        cows_in_milk=410,
    )
    assert result["sample_auto_filled"] is True
    assert result["rows"][0]["sample_id"] == "7788"
    row = db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")).one()
    assert row.sample_id == "7788"


def test_gad_autofill_skips_when_multiple_loads_or_nml_samples() -> None:
    from app.models import NmlMilkResult

    db = _session()
    day = dt.date(2026, 8, 13)
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1001",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1002",
        )
    )
    db.commit()

    result = create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[{"volume_litres": 12000, "temp_c": 3.1}],
    )
    assert result["sample_auto_filled"] is False
    assert result["rows"][0]["sample_id"] == ""

    day2 = dt.date(2026, 8, 14)
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day2,
            sample_id="2002",
        )
    )
    db.commit()
    result2 = create_manual_collection(
        db,
        collection_date=day2,
        farm="GAD",
        loads=[
            {"volume_litres": 8000, "temp_c": 3.0},
            {"volume_litres": 7000, "temp_c": 3.1},
        ],
    )
    assert result2["sample_auto_filled"] is False
    assert all(r["sample_id"] == "" for r in result2["rows"])


def test_gad_autofill_pairs_multi_load_by_volume() -> None:
    from app.models import NmlMilkResult

    db = _session()
    day = dt.date(2026, 8, 16)
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1002",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="1001",
        )
    )
    db.commit()

    result = create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 8000, "temp_c": 3.0},
            {"volume_litres": 12000, "temp_c": 3.1},
        ],
    )
    assert result["sample_auto_filled"] is True
    by_volume = {r["volume_litres"]: r["sample_id"] for r in result["rows"]}
    assert by_volume[12000] == "1001"
    assert by_volume[8000] == "1002"
    rows = db.scalars(
        select(MilkCollection)
        .where(MilkCollection.farm == "GAD")
        .order_by(MilkCollection.arrival_time)
    ).all()
    assert rows[0].volume_litres == 8000
    assert rows[0].sample_id == "1002"
    assert rows[1].volume_litres == 12000
    assert rows[1].sample_id == "1001"


def test_get_manual_day_suggests_blank_gad_sample() -> None:
    from app.models import NmlMilkResult

    db = _session()
    day = dt.date(2026, 8, 15)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[{"volume_litres": 11000, "temp_c": 3.2}],
    )
    # NML arrives after the collection was saved without a sample
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="5566",
        )
    )
    db.commit()
    # Clear sample so edit path can suggest (create would have left it blank already)
    row = db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")).one()
    assert row.sample_id is None

    day_data = get_manual_collection_day(db, farm="GAD", collection_date=day)
    assert day_data["sample_suggested"] is True
    assert day_data["loads"][0]["sample_id"] == "5566"


def test_get_manual_day_suggests_gad_samples_by_volume() -> None:
    from app.models import NmlMilkResult

    db = _session()
    day = dt.date(2026, 8, 17)
    create_manual_collection(
        db,
        collection_date=day,
        farm="GAD",
        loads=[
            {"volume_litres": 8000, "temp_c": 3.0},
            {"volume_litres": 12000, "temp_c": 3.2},
        ],
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="2002",
        )
    )
    db.add(
        NmlMilkResult(
            farm="GAD",
            producer_ref="9131",
            sample_date=day,
            sample_id="2001",
        )
    )
    db.commit()
    for row in db.scalars(select(MilkCollection).where(MilkCollection.farm == "GAD")):
        row.sample_id = None
    db.commit()

    day_data = get_manual_collection_day(db, farm="GAD", collection_date=day)
    assert day_data["sample_suggested"] is True
    by_volume = {
        load["volume_litres"]: load["sample_id"]
        for load in day_data["loads"]
        if load.get("volume_litres")
    }
    assert by_volume[12000] == "2001"
    assert by_volume[8000] == "2002"
