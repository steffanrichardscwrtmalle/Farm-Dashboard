"""Home production summary (rolling short / 30d) averages."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MilkCollection, NmlMilkResult
from app.services.production_summary import (
    _blend_metric_windows,
    _cap_iqr_points,
    _metric_window,
    get_production_summary,
)


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
    scc: int | None = None,
    bactoscan: int | None = None,
    temp: float | None = None,
) -> None:
    db.add(
        MilkCollection(
            farm=farm,
            collection_date=day,
            sample_id=sample_id,
            volume_litres=volume,
            cows_in_milk=cows,
            temp_c=temp,
            source_file="test",
        )
    )
    if fat is not None or protein is not None or scc is not None or bactoscan is not None:
        db.add(
            NmlMilkResult(
                farm=farm,
                producer_ref=f"{farm}-P",
                sample_date=day,
                sample_id=sample_id,
                butterfat_pct=fat,
                protein_pct=protein,
                scc=scc,
                bactoscan=bactoscan,
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


def test_blend_metric_windows_averages_6_and_7() -> None:
    # Perfect 3/4-load alternation: 84k / 112k → 6d flat at 98k; 7d wobbles.
    points = [
        {"date": f"2026-08-{day:02d}", "volume_litres": 84000 if day % 2 else 112000}
        for day in range(1, 10)
    ]
    blended = _blend_metric_windows(
        points, key="volume_litres", windows=(6, 7), dp=0, as_int=True
    )
    six = _metric_window(points, key="volume_litres", days=6, dp=0, as_int=True)
    seven = _metric_window(points, key="volume_litres", days=7, dp=0, as_int=True)
    assert six["value"] == 98000
    assert seven["value"] != six["value"]
    assert blended["value"] == round((six["value"] + seven["value"]) / 2)
    assert blended["days"] == "rolling"
    assert blended["to"] == "2026-08-09"


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
        scc=140,
        bactoscan=22,
        temp=3.6,
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
        scc=160,
        bactoscan=28,
        temp=3.8,
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
        scc=150,
        bactoscan=20,
        temp=3.7,
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
    assert cm["d7"]["days"] == "rolling"
    assert cm["d7"]["to"] == "2026-08-09"
    assert cm["d7"]["from"] == "2026-08-03"
    assert cm["d7"]["milk_per_day"] == 19000
    assert cm["d7"]["butterfat_pct"] == 4.1
    assert cm["d7"]["protein_pct"] == 3.3
    assert cm["d7"]["scc"] == 150
    assert cm["d7"]["bactoscan"] == 22
    assert cm["d7"]["milk_temp"] == 3.7
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


def test_cap_iqr_clamps_quality_outliers_not_volume() -> None:
    fats = [4.0, 4.05, 3.95, 4.1, 4.0, 3.9, 4.2, 12.0]
    volumes = [18000, 19000, 18500, 20000, 19500, 18800, 19200, 999999]
    points = [
        {
            "date": f"2026-08-{day:02d}",
            "volume_litres": volumes[day - 1],
            "butterfat_pct": fats[day - 1],
            "litres_per_cow": 36.0 if day < 8 else 200.0,
        }
        for day in range(1, 9)
    ]
    capped = _cap_iqr_points(points)
    assert capped[-1]["volume_litres"] == 999999
    assert capped[-1]["litres_per_cow"] == 200.0
    assert capped[-1]["butterfat_pct"] < 12.0
    assert all(row["butterfat_pct"] == fats[i] for i, row in enumerate(capped[:-1]))
    window = _metric_window(capped, key="butterfat_pct", days=8, dp=2)
    raw = _metric_window(points, key="butterfat_pct", days=8, dp=2)
    assert window["value"] < raw["value"]


def test_cap_iqr_leaves_short_series_unchanged() -> None:
    points = [
        {"date": "2026-08-01", "butterfat_pct": 4.0, "scc": 120},
        {"date": "2026-08-02", "butterfat_pct": 9.9, "scc": 800},
        {"date": "2026-08-03", "butterfat_pct": 4.1, "scc": 130},
    ]
    assert _cap_iqr_points(points) == points
