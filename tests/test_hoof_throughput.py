"""Unique-cow FOOTRIM/LAME throughput: day / week / month rollups."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent
from app.services.events_common import (
    _build_footrim_throughput,
    _resolve_throughput_dates,
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
    event: str = "FOOTRIM",
    etag: str | None = None,
) -> CowEvent:
    return CowEvent(
        farm=farm,
        cow_id=cow_id,
        etag=etag or f"UK{cow_id}",
        event=event,
        event_date=event_date,
        month_label=event_date.strftime("%b-%y"),
        fiscal_year=event_date.year + 1 if event_date.month >= 4 else event_date.year,
    )


def _footrim(**kwargs) -> CowEvent:
    return _event(event="FOOTRIM", **kwargs)


def _throughput(session: Session, **kwargs):
    defaults = dict(
        selected_farms=["CM", "GAD"],
        effective_from=dt.date(2026, 4, 1),
        effective_to=dt.date(2026, 4, 30),
    )
    defaults.update(kwargs)
    return _build_footrim_throughput(session, **defaults)


def test_same_cow_two_footrim_rows_same_day_counts_once(db: Session) -> None:
    monday = dt.date(2026, 4, 6)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=monday),
            _footrim(farm="CM", cow_id="101", event_date=monday),
        ]
    )
    db.commit()
    result = _throughput(db)
    assert result["summary"]["unique_cows"] == 1
    assert result["summary"]["trimming_days"] == 1
    assert result["day_rows"][0]["total"] == 1
    assert result["day_rows"][0]["CM"] == 1


def test_same_cow_two_days_in_one_week_is_unique_for_the_week(db: Session) -> None:
    monday = dt.date(2026, 4, 6)
    tuesday = dt.date(2026, 4, 7)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=monday),
            _footrim(farm="CM", cow_id="101", event_date=tuesday),
        ]
    )
    db.commit()
    result = _throughput(db)
    daily_totals = {row["date"]: row["total"] for row in result["day_rows"]}
    assert daily_totals[monday.isoformat()] == 1
    assert daily_totals[tuesday.isoformat()] == 1
    assert result["summary"]["unique_cows"] == 1
    assert result["summary"]["trimming_days"] == 2
    assert result["summary"]["average_per_trimming_day"] == 1.0
    assert len(result["week_rows"]) == 1
    assert result["week_rows"][0]["total"] == 1
    assert result["week_rows"][0]["CM"] == 1
    assert len(result["month_rows"]) == 1
    assert result["month_rows"][0]["total"] == 1


def test_cm_and_gad_are_kept_separate(db: Session) -> None:
    day = dt.date(2026, 4, 6)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=day),
            _footrim(farm="GAD", cow_id="202", event_date=day),
            _footrim(farm="GAD", cow_id="203", event_date=day),
        ]
    )
    db.commit()
    result = _throughput(db)
    assert result["day_rows"][0]["CM"] == 1
    assert result["day_rows"][0]["GAD"] == 2
    assert result["day_rows"][0]["total"] == 3
    assert result["summary"]["unique_cows"] == 3
    assert result["summary"]["CM"]["unique_cows"] == 1
    assert result["summary"]["GAD"]["unique_cows"] == 2


def test_average_busy_day_only_counts_days_above_10(db: Session) -> None:
    quiet = dt.date(2026, 4, 6)
    exactly_ten = dt.date(2026, 4, 7)
    busy_a = dt.date(2026, 4, 8)
    busy_b = dt.date(2026, 4, 9)
    db.add_all(
        [_footrim(farm="CM", cow_id=str(i), event_date=quiet) for i in range(5)]
        + [_footrim(farm="CM", cow_id=str(i), event_date=exactly_ten) for i in range(10)]
        + [_footrim(farm="CM", cow_id=str(i), event_date=busy_a) for i in range(12)]
        + [_footrim(farm="CM", cow_id=str(i), event_date=busy_b) for i in range(14)]
    )
    db.commit()
    result = _throughput(db)
    assert result["summary"]["average_busy_day"] == 13.0
    assert result["summary"]["busy_days"] == 2
    assert "peak_day" not in result["summary"]


def test_lame_or_footrim_counts_and_same_day_is_once(db: Session) -> None:
    day = dt.date(2026, 4, 6)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=day),
            _event(farm="CM", cow_id="101", event_date=day, event="LAME"),
            _event(farm="CM", cow_id="202", event_date=day, event="LAME"),
        ]
    )
    db.commit()
    result = _throughput(db)
    assert result["summary"]["unique_cows"] == 2
    assert result["day_rows"][0]["CM"] == 2
    assert result["day_rows"][0]["total"] == 2


def test_resolve_throughput_dates_defaults_to_last_30_days() -> None:
    today = dt.date(2026, 8, 31)
    assert _resolve_throughput_dates(None, None, today=today) == (
        dt.date(2026, 8, 2),
        dt.date(2026, 8, 31),
    )


def test_throughput_range_is_independent_of_page_dates(db: Session) -> None:
    page_day = dt.date(2025, 6, 10)
    recent = dt.date(2026, 8, 20)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=page_day),
            _footrim(farm="CM", cow_id="202", event_date=recent),
        ]
    )
    db.commit()
    result = build_events_page_report(
        db,
        page_slug="hooftrimming",
        farms=["CM"],
        event_from=dt.date(2025, 4, 1),
        event_to=dt.date(2025, 6, 30),
        throughput_from=dt.date(2026, 8, 1),
        throughput_to=dt.date(2026, 8, 31),
    )
    assert result["footrim_throughput"]["summary"]["unique_cows"] == 1
    assert result["footrim_throughput"]["day_rows"][0]["date"] == recent.isoformat()
    assert result["footrim_throughput"]["date_from"] == "2026-08-01"
    assert result["footrim_throughput"]["date_to"] == "2026-08-31"
    assert result["grand_total"]["CM"] == 1


def test_omitted_throughput_dates_use_last_30_days(db: Session) -> None:
    today = dt.date.today()
    inside = today - dt.timedelta(days=10)
    outside = today - dt.timedelta(days=40)
    db.add_all(
        [
            _footrim(farm="CM", cow_id="101", event_date=inside),
            _footrim(farm="CM", cow_id="202", event_date=outside),
        ]
    )
    db.commit()
    result = build_events_page_report(
        db,
        page_slug="hooftrimming",
        farms=["CM"],
    )
    assert result["footrim_throughput"]["summary"]["unique_cows"] == 1
    assert result["footrim_throughput"]["date_from"] == (
        today - dt.timedelta(days=29)
    ).isoformat()
    assert result["footrim_throughput"]["date_to"] == today.isoformat()
