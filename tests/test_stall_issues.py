"""Stall Issues matrix aggregation."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ParlourMilkFlowImport, ParlourMilkFlowRow
from app.services.parlour_shift_summary import (
    _annotate_milking_point_outliers,
    list_stall_issues,
    list_stall_metric_history,
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
            for cow_n in range(8):
                _add_cow(
                    session,
                    import_id=import_id,
                    farm="CM",
                    milking_date=prev,
                    shift=shift,
                    milking_point=point,
                    start_seconds=1000 + i * 60 + cow_n,
                    yield_kg=30.0,
                    flow_120s=2000.0,
                )
        for cow_n in range(8):
            _add_cow(
                session,
                import_id=import_id,
                farm="CM",
                milking_date=prev,
                shift=shift,
                milking_point=30,
                start_seconds=200 + cow_n,
                yield_kg=5.0,
                flow_120s=400.0,
            )

    for shift in ("Morning", "Day", "Night"):
        import_id = _ensure_import(
            session, farm="CM", milking_date=day, shift=shift
        )
        for i, point in enumerate(peer_points):
            for cow_n in range(8):
                _add_cow(
                    session,
                    import_id=import_id,
                    farm="CM",
                    milking_date=day,
                    shift=shift,
                    milking_point=point,
                    start_seconds=1000 + i * 60 + cow_n,
                    yield_kg=30.0,
                    flow_120s=2000.0,
                )
        for cow_n in range(8):
            _add_cow(
                session,
                import_id=import_id,
                farm="CM",
                milking_date=day,
                shift=shift,
                milking_point=30,
                start_seconds=200 + cow_n,
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

    detail = list_stall_metric_history(
        session,
        farm="CM",
        milking_point=30,
        date_from=day,
        date_to=day,
    )
    assert detail["milking_point"] == 30
    assert detail["dates"] == ["2026-07-30"]
    morning = detail["cells"]["2026-07-30"]["Morning"]
    flags = morning.get("outlier_flags") or {}
    assert any(flag == "problem" for flag in flags.values())
    assert flags.get("avg_yield_kg") == "problem"
    yield_stats = detail["stats"]["avg_yield_kg"]
    assert yield_stats["mean"] is not None
    assert yield_stats["mean"] > float(morning["avg_yield_kg"])
    assert yield_stats["sd"] is not None

    try:
        list_stall_metric_history(
            session,
            farm="CM",
            milking_point=30,
            date_from=day - dt.timedelta(days=7),
            date_to=day,
        )
        raise AssertionError("expected ValueError for span over 4 days")
    except ValueError as exc:
        assert "4 days" in str(exc)

    session.close()


def _stall(point: int, **metrics: float) -> dict:
    row: dict = {"milking_point": point, "cow_count": 8, "avg_yield_kg": 30.0}
    row.update(metrics)
    return row


def _two_shifts(bad: dict, peers: list[dict]) -> dict:
    day = dt.date(2026, 8, 1)
    prev = day - dt.timedelta(days=1)
    by_shift = {
        (prev, "Night"): [dict(peer) for peer in peers] + [dict(bad)],
        (day, "Morning"): [dict(peer) for peer in peers] + [dict(bad)],
    }
    _annotate_milking_point_outliers(by_shift)
    morning_bad = by_shift[(day, "Morning")][-1]
    return morning_bad.get("outlier_flags") or {}


def test_tight_yield_gap_is_not_a_problem() -> None:
    peers = [_stall(point) for point in range(1, 6)]
    flags = _two_shifts(_stall(9, avg_yield_kg=29.6), peers)
    assert "avg_yield_kg" not in flags


def test_meaningful_yield_gap_repeated_is_a_problem() -> None:
    peers = [_stall(point) for point in range(1, 6)]
    flags = _two_shifts(_stall(9, avg_yield_kg=28.0), peers)
    assert flags.get("avg_yield_kg") == "problem"


def test_let_down_repeats_across_different_flow_metrics() -> None:
    day = dt.date(2026, 8, 1)
    prev = day - dt.timedelta(days=1)
    peers = [
        _stall(point, avg_flow_15s=2000.0, avg_flow_30s=2000.0)
        for point in range(1, 6)
    ]
    by_shift = {
        (prev, "Night"): peers + [_stall(9, avg_flow_15s=1000.0, avg_flow_30s=2000.0)],
        (day, "Morning"): [
            _stall(point, avg_flow_15s=2000.0, avg_flow_30s=2000.0)
            for point in range(1, 6)
        ] + [_stall(9, avg_flow_15s=2000.0, avg_flow_30s=1000.0)],
    }
    _annotate_milking_point_outliers(by_shift)
    flags = by_shift[(day, "Morning")][-1]["outlier_flags"]
    assert flags.get("avg_flow_30s") == "problem"
    assert "avg_flow_15s" not in flags


def test_a_different_fault_on_the_next_shift_is_only_an_alert() -> None:
    day = dt.date(2026, 8, 1)
    prev = day - dt.timedelta(days=1)
    peers = [_stall(point, avg_flow_15s=2000.0) for point in range(1, 6)]
    by_shift = {
        (prev, "Night"): peers + [_stall(9, avg_yield_kg=20.0, avg_flow_15s=2000.0)],
        (day, "Morning"): [
            _stall(point, avg_flow_15s=2000.0) for point in range(1, 6)
        ] + [_stall(9, avg_yield_kg=30.0, avg_flow_15s=1000.0)],
    }
    _annotate_milking_point_outliers(by_shift)
    flags = by_shift[(day, "Morning")][-1]["outlier_flags"]
    assert flags.get("avg_flow_15s") == "alert"
    assert "avg_yield_kg" not in flags


def test_stall_with_too_few_cows_is_not_flagged() -> None:
    peers = [_stall(point) for point in range(1, 6)]
    flags = _two_shifts(_stall(9, avg_yield_kg=10.0, cow_count=3), peers)
    assert flags == {}
