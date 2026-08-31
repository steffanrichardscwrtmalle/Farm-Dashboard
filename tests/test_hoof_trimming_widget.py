"""Home hoof-trimming 7-day unique-cow widget tests."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CowEvent
from app.services.hoof_trimming_widget import get_hoof_trimming_7d


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _event(
    *,
    farm: str,
    cow_id: str,
    event_date: dt.date,
    event: str = "FOOTRIM",
) -> CowEvent:
    return CowEvent(
        farm=farm,
        cow_id=cow_id,
        etag=f"UK{cow_id}",
        event=event,
        event_date=event_date,
        month_label=event_date.strftime("%b-%y"),
        fiscal_year=event_date.year + 1 if event_date.month >= 4 else event_date.year,
    )


def test_counts_unique_cows_yesterday_and_six_days_before() -> None:
    session = _session()
    today = dt.date(2026, 8, 31)
    # Window: Aug 24 .. Aug 30
    session.add(_event(farm="CM", cow_id="101", event_date=dt.date(2026, 8, 24)))
    session.add(_event(farm="CM", cow_id="202", event_date=dt.date(2026, 8, 30)))
    session.add(_event(farm="GAD", cow_id="303", event_date=dt.date(2026, 8, 27)))
    session.add(_event(farm="GAD", cow_id="303", event_date=dt.date(2026, 8, 28)))
    # Same cow FOOTRIM + LAME same day = one
    session.add(_event(farm="CM", cow_id="404", event_date=dt.date(2026, 8, 26)))
    session.add(
        _event(farm="CM", cow_id="404", event_date=dt.date(2026, 8, 26), event="LAME")
    )
    # Today excluded
    session.add(_event(farm="CM", cow_id="505", event_date=today))
    # Day before window excluded (Aug 23)
    session.add(_event(farm="GAD", cow_id="606", event_date=dt.date(2026, 8, 23)))
    session.commit()

    result = get_hoof_trimming_7d(session, as_of=today)
    by_farm = {row["farm"]: row["count"] for row in result["farms"]}
    assert result["from"] == "2026-08-24"
    assert result["to"] == "2026-08-30"
    assert by_farm["CM"] == 3
    assert by_farm["GAD"] == 1
    assert result["href"] == "/events/hooftrimming"
    assert result["label"] == "Hoof — 7 Days"
