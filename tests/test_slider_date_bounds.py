"""No-empty-future caps for date/range sliders."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, HerdBirth
from app.services.births_report import build_births_report
from app.services.events_common import (
    _cap_slider_max,
    _resolve_slider_bounds,
    build_events_report,
)
from app.services.stp_report import build_stp_report


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_cap_slider_max_blocks_empty_future() -> None:
    today = dt.date(2025, 11, 27)
    fy_end = dt.date(2026, 3, 31)
    assert _cap_slider_max(fy_end, today=today, allow_future=False) == today
    assert (
        _cap_slider_max(
            fy_end,
            data_max=dt.date(2025, 10, 1),
            today=today,
            allow_future=True,
        )
        == today
    )


def test_cap_slider_max_allows_future_when_data_exists() -> None:
    today = dt.date(2025, 11, 27)
    fy_end = dt.date(2026, 3, 31)
    data_max = dt.date(2026, 2, 15)
    assert (
        _cap_slider_max(
            fy_end,
            data_max=data_max,
            today=today,
            allow_future=True,
        )
        == data_max
    )
    # Without allow_future, even future data is clipped to today.
    assert (
        _cap_slider_max(
            fy_end,
            data_max=data_max,
            today=today,
            allow_future=False,
        )
        == today
    )


def test_resolve_slider_bounds_caps_current_fiscal_year() -> None:
    today = dt.date(2025, 11, 27)
    slider_min, slider_max = _resolve_slider_bounds(
        dt.date(2024, 1, 1),
        dt.date(2025, 10, 1),
        fiscal_year=2026,
        today=today,
        allow_future=False,
    )
    assert slider_min == dt.date(2025, 4, 1)
    assert slider_max == today


def test_events_sales_slider_caps_at_today() -> None:
    session = _db()
    today = dt.date(2025, 11, 27)
    session.add(
        CowEvent(
            farm="CM",
            cow_id="1",
            event="SOLD",
            event_date=dt.date(2025, 5, 10),
            fiscal_year=2026,
        )
    )
    session.commit()
    report = build_events_report(
        session,
        event_types=("SOLD",),
        farms=["CM"],
        fiscal_year=2026,
        today=today,
        allow_future=False,
        bounds_event_types=("SOLD", "DIED"),
    )
    assert report["date_bounds"] == {"min": "2025-04-01", "max": "2025-11-27"}


def test_events_calvings_allows_future_data() -> None:
    session = _db()
    today = dt.date(2025, 11, 27)
    session.add_all(
        [
            CowEvent(
                farm="CM",
                cow_id="1",
                event="FRESH",
                event_date=dt.date(2025, 5, 10),
                fiscal_year=2026,
            ),
            CowEvent(
                farm="CM",
                cow_id="2",
                event="FRESH",
                event_date=dt.date(2026, 2, 20),
                fiscal_year=2026,
            ),
        ]
    )
    session.commit()
    report = build_events_report(
        session,
        event_types=("FRESH",),
        farms=["CM"],
        fiscal_year=2026,
        today=today,
        allow_future=True,
    )
    assert report["date_bounds"] == {"min": "2025-04-01", "max": "2026-02-20"}


def test_births_allows_future_data_within_fy() -> None:
    session = _db()
    today = dt.date(2025, 11, 27)
    session.add(
        HerdBirth(
            farm="CM",
            cow_id="1",
            etag="1",
            bdat=dt.date(2026, 1, 15),
            fiscal_year=2026,
            category="Dairy",
            import_timestamp=dt.datetime(2025, 11, 1),
        )
    )
    session.commit()
    report = build_births_report(
        session, farms=["CM"], fiscal_year=2026, today=today
    )
    assert report["date_bounds"] == {"min": "2025-04-01", "max": "2026-01-15"}


def test_stp_caps_fy_max_but_keeps_90_day_default() -> None:
    session = _db()
    today = dt.date(2025, 11, 27)
    session.add(
        CowEvent(
            farm="CM",
            cow_id="1",
            etag="1",
            event="STP",
            lact=0,
            bdat=dt.date(2025, 10, 1),
            event_date=dt.date(2025, 10, 2),
            fiscal_year=2026,
            remark="6.0",
            cbrd=1,
            import_timestamp=dt.datetime(2025, 10, 3),
        )
    )
    session.commit()
    report = build_stp_report(session, farms=["CM"], today=today)
    assert report["birth_bounds"] == {"min": "2025-04-01", "max": "2025-11-27"}
    assert report["birth_to"] == "2025-10-01"
    assert report["birth_from"] == "2025-07-03"
    assert report["default_lookback_days"] == 90
