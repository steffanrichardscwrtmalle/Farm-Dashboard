"""Home production summary (7d / 30d) averages."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection, NmlMilkResult
from app.services.production_summary import _metric_window, get_production_summary


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add_load(
    db,
    *,
    farm: str,
    day: dt.date,
    sample_id: str,
    volume: int,
    cows: int | None = None,
    fat: float | None = None,
    protein: float | None = None,
) -> None:
    db.add(
        MilkCollection(
            farm=farm,
            collection_date=day,
            sample_id=sample_id,
            volume_litres=volume,
            cows_in_milk=cows,
            source_file="test",
        )
    )
    if fat is not None or protein is not None:
        db.add(
            NmlMilkResult(
                farm=farm,
                producer_ref=f"{farm}-P",
                sample_date=day,
                sample_id=sample_id,
                butterfat_pct=fat,
                protein_pct=protein,
            )
        )


def test_metric_window_ends_on_latest_day_with_that_metric() -> None:
    points = [
        {
            "date": "2026-08-03",
            "volume_litres": 10000,
            "butterfat_pct": 4.0,
        },
        {
            "date": "2026-08-05",
            "volume_litres": 12000,
            "butterfat_pct": 4.2,
        },
        {
            "date": "2026-08-09",
            "volume_litres": 14000,
            # no fat yet on the latest volume day
        },
        {
            "date": "2026-08-02",
            "volume_litres": 99999,
            "butterfat_pct": 9.9,
        },
    ]
    volume = _metric_window(points, key="volume_litres", days=7, dp=0, as_int=True)
    assert volume["to"] == "2026-08-09"
    assert volume["from"] == "2026-08-03"
    assert volume["days_with_data"] == 3
    assert volume["value"] == 12000  # (10000+12000+14000)/3

    fat = _metric_window(points, key="butterfat_pct", days=7, dp=2)
    # Fat window ends on Aug 5 (latest day with fat), not Aug 9
    assert fat["to"] == "2026-08-05"
    assert fat["from"] == "2026-07-30"
    # Aug 2/3/5 all have fat inside that window
    assert fat["days_with_data"] == 3
    assert fat["value"] == 6.03  # (9.9+4.0+4.2)/3


def test_production_summary_ignores_trailing_empty_days_after_latest() -> None:
    db = _session()
    as_of = dt.date(2026, 8, 10)
    # CM: latest volume Aug 9 — as_of Aug 10 has no data and must not shift the window
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 9),
        sample_id="1",
        volume=11000,
        cows=500,
        fat=4.10,
        protein=3.30,
    )
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 9),
        sample_id="2",
        volume=9000,
        cows=500,
        fat=4.30,
        protein=3.50,
    )
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 8),
        sample_id="3",
        volume=18000,
        cows=500,
        fat=4.00,
        protein=3.20,
    )
    # GAD: only Aug 7 — window ends there even though as_of is Aug 10
    _add_load(
        db,
        farm="GAD",
        day=dt.date(2026, 8, 7),
        sample_id="10",
        volume=15000,
        cows=400,
        fat=3.90,
        protein=3.10,
    )
    db.commit()

    result = get_production_summary(db, as_of=as_of)
    assert result["as_of"] == "2026-08-10"
    by_farm = {row["farm"]: row for row in result["farms"]}

    cm = by_farm["CM"]
    assert cm["window_end"] == "2026-08-09"
    assert cm["d7"]["to"] == "2026-08-09"
    assert cm["d7"]["from"] == "2026-08-03"
    assert cm["d7"]["milk_per_day"] == 19000
    assert cm["d7"]["butterfat_pct"] == 4.1
    assert cm["d7"]["protein_pct"] == 3.3
    assert cm["d30"]["milk_per_day"] == 19000

    gad = by_farm["GAD"]
    assert gad["window_end"] == "2026-08-07"
    assert gad["d7"]["to"] == "2026-08-07"
    assert gad["d7"]["from"] == "2026-08-01"
    assert gad["d7"]["milk_per_day"] == 15000
    assert gad["d7"]["butterfat_pct"] == 3.9


def test_production_summary_empty_farm() -> None:
    db = _session()
    result = get_production_summary(db, as_of=dt.date(2026, 8, 10))
    for farm_row in result["farms"]:
        assert farm_row["window_end"] is None
        assert farm_row["d7"]["milk_per_day"] is None
        assert farm_row["d30"]["milk_per_cow"] is None
