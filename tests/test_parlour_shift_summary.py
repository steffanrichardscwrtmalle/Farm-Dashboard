"""Shift summary aggregation (memory-safe streaming path)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_shift_summary import (
    MAX_SHIFT_SUMMARY_SPAN_DAYS,
    list_shift_summaries,
    resolve_shift_summary_dates,
)


def _ensure_import(
    session,
    *,
    farm: str,
    milking_date: dt.date,
    shift: str,
) -> int:
    batch = ParlourMilkFlowImport(
        farm=farm,
        milking_date=milking_date,
        shift=shift,
        source_filename=f"{farm}-{milking_date}-{shift}.xls",
        rows_imported=0,
    )
    session.add(batch)
    session.flush()
    return batch.id


def _add_cow(
    session,
    *,
    import_id: int,
    farm: str,
    milking_date: dt.date,
    shift: str,
    cow_id: str,
    milking_point: int,
    start_seconds: int,
    yield_kg: float,
    pen: int | None = 1,
) -> None:
    session.add(
        ParlourMilkFlowRow(
            import_id=import_id,
            farm=farm,
            milking_date=milking_date,
            shift=shift,
            cow_id=cow_id,
            pen=pen,
            milking_point=milking_point,
            start_seconds=start_seconds,
            duration_seconds=400,
            yield_kg=yield_kg,
            flow_15s=1.0,
            flow_30s=2.0,
            flow_60s=3.0,
            flow_120s=4.0,
            average_flow=2.5,
            peak_flow=4.0,
            flow_rate_at_removal=500.0,
            pct_2_minutes=50.0,
            milk_yield_2_minutes=2.0,
        )
    )


def test_resolve_shift_summary_dates_defaults_and_caps() -> None:
    today = dt.date.today()
    start, end = resolve_shift_summary_dates(None, None)
    assert end == today
    assert start == today - dt.timedelta(days=MAX_SHIFT_SUMMARY_SPAN_DAYS)

    with pytest.raises(ValueError, match="cannot exceed"):
        resolve_shift_summary_dates(
            today - dt.timedelta(days=MAX_SHIFT_SUMMARY_SPAN_DAYS + 1),
            today,
        )

    with pytest.raises(ValueError, match="Pen breakdown"):
        resolve_shift_summary_dates(
            today - dt.timedelta(days=10),
            today,
            include_pens=True,
        )


def test_list_shift_summaries_streams_by_shift() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    day = dt.date(2026, 7, 20)
    for shift_name, start0, y0, y1 in (
        ("Morning", 5 * 3600, 10.0, 12.0),
        ("Day", 13 * 3600, 11.0, 13.0),
    ):
        import_id = _ensure_import(
            session, farm="CM", milking_date=day, shift=shift_name
        )
        _add_cow(
            session,
            import_id=import_id,
            farm="CM",
            milking_date=day,
            shift=shift_name,
            cow_id=f"{shift_name}-1",
            milking_point=1,
            start_seconds=start0,
            yield_kg=y0,
        )
        _add_cow(
            session,
            import_id=import_id,
            farm="CM",
            milking_date=day,
            shift=shift_name,
            cow_id=f"{shift_name}-2",
            milking_point=2,
            start_seconds=start0 + 60,
            yield_kg=y1,
        )
    session.commit()

    payload = list_shift_summaries(
        session,
        farm="CM",
        date_from=day,
        date_to=day,
    )
    assert payload["day_count"] == 1
    day_row = payload["days"][0]
    assert day_row["milking_date"] == day.isoformat()
    assert day_row["total_cows"] == 4
    assert day_row["total_yield_kg"] == 46.0
    shifts = {s["shift"]: s for s in day_row["shifts"]}
    assert shifts["Morning"]["cow_count"] == 2
    assert shifts["Morning"]["yield_kg"] == 22.0
    assert shifts["Day"]["cow_count"] == 2
    assert shifts["Day"]["yield_kg"] == 24.0
    assert shifts["Morning"]["high_flow_takeoff_pct"] is not None


def test_list_shift_summaries_problem_stalls_uses_prior_day() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    day = dt.date(2026, 7, 21)
    prev = day - dt.timedelta(days=1)
    # Enough peer stalls for outlier stats (≥5).
    points = list(range(10, 70, 10))

    for milking_date, shift_name in ((prev, "Night"), (day, "Morning")):
        import_id = _ensure_import(
            session, farm="GAD", milking_date=milking_date, shift=shift_name
        )
        for i, point in enumerate(points):
            # Stall 10 consistently low yield → problem across shifts.
            yield_kg = 5.0 if point == 10 else 20.0 + (i * 0.1)
            _add_cow(
                session,
                import_id=import_id,
                farm="GAD",
                milking_date=milking_date,
                shift=shift_name,
                cow_id=f"{milking_date}-{shift_name}-{point}",
                milking_point=point,
                start_seconds=6 * 3600 + i * 30,
                yield_kg=yield_kg,
            )
    session.commit()

    payload = list_shift_summaries(
        session,
        farm="GAD",
        date_from=day,
        date_to=day,
        include_problem_stalls=True,
    )
    assert payload["day_count"] == 1
    morning = next(s for s in payload["days"][0]["shifts"] if s["shift"] == "Morning")
    assert morning["problem_stall_count"] is not None
    assert morning["problem_stall_count"] >= 1
