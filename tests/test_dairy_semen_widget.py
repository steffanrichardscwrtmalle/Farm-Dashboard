"""Home dairy-semen 30-day widget tests."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, BreedingSireClassification, CowEvent
from app.services.dairy_semen_widget import _status_for_count, get_dairy_semen_30d


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_status_bands() -> None:
    assert _status_for_count(0) == "red"
    assert _status_for_count(370) == "red"
    assert _status_for_count(371) == "yellow"
    assert _status_for_count(390) == "yellow"
    assert _status_for_count(391) == "green"
    assert _status_for_count(420) == "green"
    assert _status_for_count(421) == "purple"
    assert _status_for_count(500) == "purple"


def test_counts_dairy_only_both_farms_excluding_today() -> None:
    session = _session()
    today = dt.date(2026, 8, 7)
    # Inside window (today-30 .. today-1): Jul 8 .. Aug 6
    session.add(
        CowEvent(
            farm="CM",
            etag="UK1",
            event="BRED",
            event_date=dt.date(2026, 7, 8),
            remark="SIRE1.s",
        )
    )
    session.add(
        CowEvent(
            farm="GAD",
            etag="UK2",
            event="BRED",
            event_date=dt.date(2026, 8, 6),
            remark="SIRE2.s",
        )
    )
    # Beef — ignored
    session.add(
        CowEvent(
            farm="CM",
            etag="UK3",
            event="BRED",
            event_date=dt.date(2026, 8, 1),
            remark="SIRE3.b",
        )
    )
    # Today — excluded
    session.add(
        CowEvent(
            farm="CM",
            etag="UK4",
            event="BRED",
            event_date=today,
            remark="SIRE4.s",
        )
    )
    # Outside 30d window but inside 120d (today-120 .. today-31)
    session.add(
        CowEvent(
            farm="GAD",
            etag="UK5",
            event="BRED",
            event_date=dt.date(2026, 7, 7),
            remark="SIRE5.s",
        )
    )
    # Four more in the older part of the 120d window → +4 to 120d count
    for i, day in enumerate((10, 20, 40, 60)):
        session.add(
            CowEvent(
                farm="CM",
                etag=f"UK9{i}",
                event="BRED",
                event_date=today - dt.timedelta(days=day),
                remark=f"OLD{i}.s",
            )
        )
    # Override-classified dairy (no .s/.b suffix)
    session.add(BreedingSireClassification(sire_code="CUSTOM", semen_type="dairy"))
    session.add(
        CowEvent(
            farm="CM",
            etag="UK6",
            event="BRED",
            event_date=dt.date(2026, 8, 5),
            remark="CUSTOM",
        )
    )
    session.commit()

    result = get_dairy_semen_30d(session, as_of=today)
    # 30d: Jul 8, Aug 6, Aug 5 CUSTOM = 3 (day-10 and day-20 also in 30d)
    # day-10 = Jul 28, day-20 = Jul 18 — both in 30d window
    assert result["count"] == 5
    # 120d: 5 in/near 30d + Jul 7 + day-40 + day-60 = 8
    assert result["count_120"] == 8
    assert result["avg_30d_120"] == 2
    assert result["status"] == "red"
    assert result["avg_status"] == "red"
    assert result["from"] == "2026-07-08"
    assert result["to"] == "2026-08-06"
    assert result["from_120"] == "2026-04-09"
    assert result["href"] == "/events/breedings"
    assert result["label"] == "Dairy Semen - 30 Days"


def test_avg_status_uses_120d_average_bands() -> None:
    session = _session()
    today = dt.date(2026, 8, 7)
    # 1600 dairy services over 120d → avg 400 → green; keep 30d low (red widget).
    for i in range(1600):
        days_ago = 40 + (i % 80)  # all outside the 30d window
        session.add(
            CowEvent(
                farm="CM" if i % 2 == 0 else "GAD",
                etag=f"UK{i}",
                event="BRED",
                event_date=today - dt.timedelta(days=days_ago),
                remark=f"D{i}.s",
            )
        )
    session.commit()

    result = get_dairy_semen_30d(session, as_of=today)
    assert result["count"] == 0
    assert result["status"] == "red"
    assert result["avg_30d_120"] == 400
    assert result["avg_status"] == "green"
