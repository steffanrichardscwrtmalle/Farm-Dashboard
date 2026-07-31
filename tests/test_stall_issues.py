"""Stall Issues matrix aggregation."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_shift_summary import list_stall_issues


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
    milking_point: int,
    start_seconds: int,
    yield_kg: float,
    flow_120s: float,
) -> None:
    session.add(
        ParlourMilkFlowRow(
            import_id=import_id,
            farm=farm,
            milking_date=milking_date,
            shift=shift,
            cow_id=f"{milking_date}-{shift}-{milking_point}-{start_seconds}",
            milking_point=milking_point,
            start_seconds=start_seconds,
            duration_seconds=400,
            yield_kg=yield_kg,
            flow_15s=flow_120s,
            flow_30s=flow_120s,
            flow_60s=flow_120s,
            flow_120s=flow_120s,
            average_flow=min(flow_120s / 60.0, 8.0),
            peak_flow=min(flow_120s / 50.0, 9.0),
            flow_rate_at_removal=500.0,
        )
    )


def test_list_stall_issues_counts_problem_shifts_per_day() -> None:
    """Stall 30 problem on all three shifts of one day → cell value 3."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    day = dt.date(2026, 7, 30)
    prev = day - dt.timedelta(days=1)
    peer_points = [10, 20, 40, 50, 60]

    for shift in ("Night",):
        import_id = _ensure_import(
            session, farm="CM", milking_date=prev, shift=shift
        )
        for i, point in enumerate(peer_points):
            _add_cow(
                session,
                import_id=import_id,
                farm="CM",
                milking_date=prev,
                shift=shift,
                milking_point=point,
                start_seconds=1000 + i * 60,
                yield_kg=30.0,
                flow_120s=2000.0,
            )
        _add_cow(
            session,
            import_id=import_id,
            farm="CM",
            milking_date=prev,
            shift=shift,
            milking_point=30,
            start_seconds=200,
            yield_kg=5.0,
            flow_120s=400.0,
        )

    for shift in ("Morning", "Day", "Night"):
        import_id = _ensure_import(
            session, farm="CM", milking_date=day, shift=shift
        )
        for i, point in enumerate(peer_points):
            _add_cow(
                session,
                import_id=import_id,
                farm="CM",
                milking_date=day,
                shift=shift,
                milking_point=point,
                start_seconds=1000 + i * 60,
                yield_kg=30.0,
                flow_120s=2000.0,
            )
        _add_cow(
            session,
            import_id=import_id,
            farm="CM",
            milking_date=day,
            shift=shift,
            milking_point=30,
            start_seconds=200,
            yield_kg=5.0,
            flow_120s=400.0,
        )

    session.commit()

    result = list_stall_issues(
        session,
        farm="CM",
        date_from=day,
        date_to=day,
    )
    assert result["dates"] == ["2026-07-30"]
    stall_30 = next(r for r in result["rows"] if r["milking_point"] == 30)
    assert stall_30["by_date"]["2026-07-30"] == 3
    assert stall_30["total"] == 3

    session.close()
