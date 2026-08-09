"""Haulier import prunes re-dated loads within a month snapshot."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection
from app.services.haulier_import import _prune_stale_month_rows, _row_key, _upsert


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_prune_removes_loads_left_on_wrong_date_after_redate() -> None:
    db = _session()
    # Previous buggy import: samples 093-096 all sitting on the 24th.
    for sample, volume in (("090", 28173), ("091", 28015), ("093", 28217), ("094", 28222)):
        db.add(
            MilkCollection(
                farm="CM",
                collection_date=dt.date(2026, 7, 24),
                sample_id=sample,
                volume_litres=volume,
                source_file="Cwrt Malle - 07 July 2026.xlsx",
                source_message_id="msg-july",
                source_received=dt.datetime(2026, 8, 1, 8, 0, 0),
            )
        )
    db.commit()

    # Corrected parse: 090/091 stay on 24th; 093/094 move to 25th.
    parsed = {
        _row_key("CM", dt.date(2026, 7, 24), "090", None): {
            "farm": "CM",
            "collection_date": dt.date(2026, 7, 24),
            "sample_id": "090",
            "volume_litres": 28173,
            "source_file": "Cwrt Malle - 07 July 2026.xlsx",
            "source_message_id": "msg-july",
            "source_received": dt.datetime(2026, 8, 1, 8, 0, 0),
        },
        _row_key("CM", dt.date(2026, 7, 24), "091", None): {
            "farm": "CM",
            "collection_date": dt.date(2026, 7, 24),
            "sample_id": "091",
            "volume_litres": 28015,
            "source_file": "Cwrt Malle - 07 July 2026.xlsx",
            "source_message_id": "msg-july",
            "source_received": dt.datetime(2026, 8, 1, 8, 0, 0),
        },
        _row_key("CM", dt.date(2026, 7, 25), "093", None): {
            "farm": "CM",
            "collection_date": dt.date(2026, 7, 25),
            "sample_id": "093",
            "volume_litres": 28217,
            "source_file": "Cwrt Malle - 07 July 2026.xlsx",
            "source_message_id": "msg-july",
            "source_received": dt.datetime(2026, 8, 1, 8, 0, 0),
        },
        _row_key("CM", dt.date(2026, 7, 25), "094", None): {
            "farm": "CM",
            "collection_date": dt.date(2026, 7, 25),
            "sample_id": "094",
            "volume_litres": 28222,
            "source_file": "Cwrt Malle - 07 July 2026.xlsx",
            "source_message_id": "msg-july",
            "source_received": dt.datetime(2026, 8, 1, 8, 0, 0),
        },
    }
    _upsert(db, parsed)
    removed = _prune_stale_month_rows(db, parsed)
    db.commit()

    assert removed == 2
    rows = db.scalars(
        select(MilkCollection).where(MilkCollection.farm == "CM")
    ).all()
    keys = {
        (r.collection_date, r.sample_id, r.volume_litres)
        for r in rows
    }
    assert keys == {
        (dt.date(2026, 7, 24), "090", 28173),
        (dt.date(2026, 7, 24), "091", 28015),
        (dt.date(2026, 7, 25), "093", 28217),
        (dt.date(2026, 7, 25), "094", 28222),
    }
