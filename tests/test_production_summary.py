"""Home production summary (7d / 30d) averages."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection, NmlMilkResult
from app.services.production_summary import _window_metrics, get_production_summary


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


def test_window_metrics_averages_daily_points() -> None:
    end = dt.date(2026, 8, 9)
    points = [
        {
            "date": "2026-08-03",
            "volume_litres": 10000,
            "litres_per_cow": 20.0,
            "butterfat_pct": 4.0,
            "protein_pct": 3.2,
        },
        {
            "date": "2026-08-05",
            "volume_litres": 12000,
            "litres_per_cow": 24.0,
            "butterfat_pct": 4.2,
            "protein_pct": 3.4,
        },
        {
            "date": "2026-08-09",
            "volume_litres": 14000,
            "litres_per_cow": 28.0,
            "butterfat_pct": 4.4,
            "protein_pct": 3.6,
        },
        # Outside 7d window (end-6 = Aug 3)
        {
            "date": "2026-08-02",
            "volume_litres": 99999,
            "litres_per_cow": 99.0,
            "butterfat_pct": 9.9,
            "protein_pct": 9.9,
        },
    ]
    result = _window_metrics(points, window_end=end, days=7)
    assert result["from"] == "2026-08-03"
    assert result["to"] == "2026-08-09"
    assert result["days_with_volume"] == 3
    # Mean of daily totals: (10000+12000+14000)/3
    assert result["milk_per_day"] == 12000
    assert result["milk_per_cow"] == 24.0
    assert result["butterfat_pct"] == 4.2
    assert result["protein_pct"] == 3.4


def test_production_summary_per_farm_windows_end_on_latest_volume() -> None:
    db = _session()
    as_of = dt.date(2026, 8, 10)
    # CM: two loads on Aug 9 → daily volume 20000; one load Aug 8
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 9),
        sample_id="1",
        volume=11000,
        fat=4.10,
        protein=3.30,
    )
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 9),
        sample_id="2",
        volume=9000,
        fat=4.30,
        protein=3.50,
    )
    _add_load(
        db,
        farm="CM",
        day=dt.date(2026, 8, 8),
        sample_id="3",
        volume=18000,
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
        fat=3.90,
        protein=3.10,
    )
    db.commit()

    result = get_production_summary(db, as_of=as_of)
    assert result["as_of"] == "2026-08-10"
    assert result["href"] == "/milk-quality/collections"
    by_farm = {row["farm"]: row for row in result["farms"]}
    assert set(by_farm) == {"CM", "GAD"}

    cm = by_farm["CM"]
    assert cm["window_end"] == "2026-08-09"
    # Daily means: Aug 9 vol 20000 fat (4.1+4.3)/2=4.2; Aug 8 vol 18000 fat 4.0
    assert cm["d7"]["milk_per_day"] == 19000
    assert cm["d7"]["butterfat_pct"] == 4.1
    assert cm["d7"]["protein_pct"] == 3.3
    assert cm["d7"]["from"] == "2026-08-03"
    assert cm["d30"]["milk_per_day"] == 19000

    gad = by_farm["GAD"]
    assert gad["window_end"] == "2026-08-07"
    assert gad["d7"]["to"] == "2026-08-07"
    assert gad["d7"]["from"] == "2026-08-01"
    assert gad["d7"]["milk_per_day"] == 15000
    assert gad["d7"]["butterfat_pct"] == 3.9
    assert gad["d7"]["protein_pct"] == 3.1


def test_production_summary_empty_farm() -> None:
    db = _session()
    result = get_production_summary(db, as_of=dt.date(2026, 8, 10))
    for farm_row in result["farms"]:
        assert farm_row["window_end"] is None
        assert farm_row["d7"]["milk_per_day"] is None
        assert farm_row["d30"]["milk_per_cow"] is None
