from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent
from app.services.stp_report import STP_DEFAULT_LOOKBACK_DAYS, build_stp_report


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _stp(
    *,
    farm: str,
    cow_id: str,
    bdat: dt.date,
    event_date: dt.date,
    fiscal_year: int,
    remark: str,
    cbrd: int = 1,
) -> CowEvent:
    return CowEvent(
        farm=farm,
        cow_id=cow_id,
        etag=cow_id,
        event="STP",
        lact=0,
        bdat=bdat,
        event_date=event_date,
        fiscal_year=fiscal_year,
        remark=remark,
        cbrd=cbrd,
        import_timestamp=dt.datetime(2026, 9, 1, 12, 0, 0),
    )


def test_stp_report_defaults_to_latest_birth_minus_90_days() -> None:
    session = _db()
    session.add_all(
        [
            _stp(
                farm="CM",
                cow_id="1",
                bdat=dt.date(2024, 6, 1),
                event_date=dt.date(2024, 6, 3),
                fiscal_year=2025,
                remark="6.4",
            ),
            _stp(
                farm="CM",
                cow_id="early",
                bdat=dt.date(2025, 4, 15),
                event_date=dt.date(2025, 4, 16),
                fiscal_year=2026,
                remark="6.5",
            ),
            _stp(
                farm="CM",
                cow_id="2",
                bdat=dt.date(2025, 8, 1),
                event_date=dt.date(2025, 8, 2),
                fiscal_year=2026,
                remark="5.9",
            ),
            _stp(
                farm="GAD",
                cow_id="3",
                bdat=dt.date(2025, 9, 10),
                event_date=dt.date(2025, 9, 12),
                fiscal_year=2026,
                remark="5.2",
            ),
        ]
    )
    session.commit()

    report = build_stp_report(session, farms=["CM", "GAD"], today=dt.date(2026, 10, 1))
    assert report["fiscal_year"] == 2026
    assert report["fiscal_year_options"] == [2026, 2025]
    assert report["birth_bounds"] == {"min": "2025-04-01", "max": "2026-03-31"}
    assert report["latest_birth"] == "2025-09-10"
    assert report["birth_to"] == "2025-09-10"
    assert report["birth_from"] == (
        dt.date(2025, 9, 10) - dt.timedelta(days=STP_DEFAULT_LOOKBACK_DAYS)
    ).isoformat()
    assert report["default_lookback_days"] == 90
    # Apr-15 row is outside the 90-day window; Aug + Sep remain.
    assert report["total"]["total"] == 2
    assert report["farms"]["CM"]["counts"]["good"] == 1
    assert report["farms"]["GAD"]["counts"]["fair"] == 1
    assert report["farms"]["CM"]["counts"]["excellent"] == 0


def test_stp_report_any_fiscal_year_defaults_to_90_day_window() -> None:
    session = _db()
    session.add_all(
        [
            _stp(
                farm="CM",
                cow_id="1",
                bdat=dt.date(2024, 6, 1),
                event_date=dt.date(2024, 6, 3),
                fiscal_year=2025,
                remark="6.4",
            ),
            _stp(
                farm="CM",
                cow_id="2",
                bdat=dt.date(2025, 8, 1),
                event_date=dt.date(2025, 8, 2),
                fiscal_year=2026,
                remark="5.9",
            ),
        ]
    )
    session.commit()

    report = build_stp_report(
        session,
        farms=["CM"],
        fiscal_year="any",
        today=dt.date(2026, 10, 1),
    )
    assert report["fiscal_year"] is None
    assert report["birth_bounds"] == {"min": "2024-06-01", "max": "2025-08-01"}
    assert report["latest_birth"] == "2025-08-01"
    assert report["birth_to"] == "2025-08-01"
    assert report["birth_from"] == (
        dt.date(2025, 8, 1) - dt.timedelta(days=STP_DEFAULT_LOOKBACK_DAYS)
    ).isoformat()
    # Older FY row falls outside the 90-day window.
    assert report["total"]["total"] == 1
    assert report["total"]["counts"]["good"] == 1
    assert report["total"]["counts"]["excellent"] == 0


def test_stp_report_any_with_explicit_range_spans_years() -> None:
    session = _db()
    session.add_all(
        [
            _stp(
                farm="CM",
                cow_id="1",
                bdat=dt.date(2024, 6, 1),
                event_date=dt.date(2024, 6, 3),
                fiscal_year=2025,
                remark="6.4",
            ),
            _stp(
                farm="CM",
                cow_id="2",
                bdat=dt.date(2025, 8, 1),
                event_date=dt.date(2025, 8, 2),
                fiscal_year=2026,
                remark="5.9",
            ),
        ]
    )
    session.commit()

    report = build_stp_report(
        session,
        farms=["CM"],
        fiscal_year="any",
        birth_from=dt.date(2024, 6, 1),
        birth_to=dt.date(2025, 8, 1),
        today=dt.date(2026, 10, 1),
    )
    assert report["fiscal_year"] is None
    assert report["birth_from"] == "2024-06-01"
    assert report["birth_to"] == "2025-08-01"
    assert report["total"]["total"] == 2
    assert report["total"]["counts"]["excellent"] == 1
    assert report["total"]["counts"]["good"] == 1


def test_stp_report_selected_fiscal_year_filters_rows() -> None:
    session = _db()
    session.add_all(
        [
            _stp(
                farm="CM",
                cow_id="1",
                bdat=dt.date(2024, 6, 1),
                event_date=dt.date(2024, 6, 3),
                fiscal_year=2025,
                remark="6.4",
            ),
            _stp(
                farm="CM",
                cow_id="2",
                bdat=dt.date(2025, 8, 1),
                event_date=dt.date(2025, 8, 2),
                fiscal_year=2026,
                remark="4.8",
            ),
        ]
    )
    session.commit()

    report = build_stp_report(
        session,
        farms=["CM"],
        fiscal_year=2025,
        today=dt.date(2026, 10, 1),
    )
    assert report["fiscal_year"] == 2025
    assert report["birth_bounds"] == {"min": "2024-04-01", "max": "2025-03-31"}
    assert report["latest_birth"] == "2024-06-01"
    assert report["birth_to"] == "2024-06-01"
    assert report["birth_from"] == "2024-04-01"  # clamped to FY start
    assert report["total"]["total"] == 1
    assert report["total"]["counts"]["excellent"] == 1
    assert report["total"]["counts"]["poor"] == 0
