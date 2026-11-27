"""Monthly % Footrim vs % Lame (100% stacked) for Hoof Trimming."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent
from app.services.events_common import (
    _build_footrim_lame_ratio_rows,
    _footrim_lame_pct_pair,
    build_events_page_report,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _event(
    *,
    farm: str,
    cow_id: str,
    event_date: dt.date,
    event: str,
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


def test_pct_pair_always_sums_to_100() -> None:
    assert _footrim_lame_pct_pair(9, 1) == (90.0, 10.0)
    assert _footrim_lame_pct_pair(1, 2) == (33.3, 66.7)
    assert _footrim_lame_pct_pair(0, 0) == (0.0, 0.0)
    assert _footrim_lame_pct_pair(0, 5) == (0.0, 100.0)


def test_ratio_aggregates_both_farms(db: Session) -> None:
    day = dt.date(2026, 4, 15)
    db.add_all(
        [
            _event(farm="CM", cow_id="1", event_date=day, event="FOOTRIM"),
            _event(farm="CM", cow_id="2", event_date=day, event="FOOTRIM"),
            _event(farm="GAD", cow_id="3", event_date=day, event="FOOTRIM"),
            _event(farm="GAD", cow_id="4", event_date=day, event="LAME"),
        ]
    )
    db.commit()

    result = build_events_page_report(
        db,
        page_slug="hooftrimming",
        farms=["CM", "GAD"],
        event_from=dt.date(2026, 4, 1),
        event_to=dt.date(2026, 4, 30),
    )
    rows = {row["event_month"]: row for row in result["footrim_lame_ratio_rows"]}
    april = rows["Apr-26"]
    assert april["footrim"] == 3
    assert april["lame"] == 1
    assert april["footrim_pct"] == 75.0
    assert april["lame_pct"] == 25.0
    assert april["footrim_pct"] + april["lame_pct"] == 100.0


def test_ratio_respects_single_farm_filter(db: Session) -> None:
    day = dt.date(2026, 4, 15)
    db.add_all(
        [
            _event(farm="CM", cow_id="1", event_date=day, event="FOOTRIM"),
            _event(farm="CM", cow_id="2", event_date=day, event="FOOTRIM"),
            _event(farm="CM", cow_id="3", event_date=day, event="LAME"),
            _event(farm="GAD", cow_id="4", event_date=day, event="LAME"),
            _event(farm="GAD", cow_id="5", event_date=day, event="LAME"),
            _event(farm="GAD", cow_id="6", event_date=day, event="LAME"),
        ]
    )
    db.commit()

    result = build_events_page_report(
        db,
        page_slug="hooftrimming",
        farms=["CM"],
        event_from=dt.date(2026, 4, 1),
        event_to=dt.date(2026, 4, 30),
    )
    april = {row["event_month"]: row for row in result["footrim_lame_ratio_rows"]}["Apr-26"]
    assert april["footrim"] == 2
    assert april["lame"] == 1
    assert april["footrim_pct"] == 66.7
    assert april["lame_pct"] == 33.3


def test_ratio_rows_builder_zero_fills_months() -> None:
    rows = _build_footrim_lame_ratio_rows(
        {"Apr-26": {"CM": 9, "GAD": 0}},
        {"Apr-26": {"CM": 1, "GAD": 0}},
        selected_farms=["CM", "GAD"],
        effective_from=dt.date(2026, 4, 1),
        effective_to=dt.date(2026, 5, 31),
    )
    by_month = {row["event_month"]: row for row in rows}
    assert set(by_month) == {"Apr-26", "May-26"}
    assert by_month["Apr-26"]["footrim_pct"] == 90.0
    assert by_month["Apr-26"]["lame_pct"] == 10.0
    assert by_month["May-26"]["total"] == 0
    assert by_month["May-26"]["footrim_pct"] == 0.0
    assert by_month["May-26"]["lame_pct"] == 0.0
