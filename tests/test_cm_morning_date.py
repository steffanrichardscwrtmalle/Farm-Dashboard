"""CM Morning milking_date resolution (Dataflow Date quirk)."""

from __future__ import annotations

import datetime as dt

from app.services.parlour_milk_flow_import import milking_date_from_received
from app.services.parlour_milk_flow_parse import (
    parse_milk_flow_report,
    resolve_cm_morning_date,
)


def test_resolve_keeps_raw_when_peers_match_milking_day() -> None:
    day = dt.date(2026, 8, 1)
    assert (
        resolve_cm_morning_date(day, peer_non_morning_dates={day}) == day
    )


def test_resolve_bumps_when_peers_match_next_day() -> None:
    raw = dt.date(2026, 7, 31)
    milking = dt.date(2026, 8, 1)
    assert (
        resolve_cm_morning_date(raw, peer_non_morning_dates={milking}) == milking
    )


def test_resolve_dawn_only_starts_keep_raw_without_peers() -> None:
    day = dt.date(2026, 8, 1)
    # Typical new Dataflow stamp: Date already = milking day, starts 04:00–07:00.
    assert (
        resolve_cm_morning_date(
            day,
            peer_non_morning_dates=set(),
            start_seconds_list=[4 * 3600, 5 * 3600, 6 * 3600],
        )
        == day
    )


def test_resolve_overnight_starts_bump_without_peers() -> None:
    raw = dt.date(2026, 7, 31)
    assert (
        resolve_cm_morning_date(
            raw,
            peer_non_morning_dates=set(),
            start_seconds_list=[23 * 3600, 4 * 3600, 5 * 3600],
        )
        == dt.date(2026, 8, 1)
    )


def test_parse_cm_morning_aligns_to_day_peer_in_file() -> None:
    csv = "\n".join(
        [
            "ID,Date,Shift,Yield,Pen,Duration,Cow Milking Start Time",
            "100,01/08/2026,Morning,12.5,1,00:05:00,05:00:00",
            "101,01/08/2026,Day,11.0,1,00:05:00,13:00:00",
        ]
    )
    reports = parse_milk_flow_report(
        csv.encode("utf-8"),
        filename="Milk Flow Report Export CM.csv",
        farm="CM",
    )
    by_shift = {r.shift: r.milking_date for r in reports}
    assert by_shift["Morning"] == dt.date(2026, 8, 1)
    assert by_shift["Day"] == dt.date(2026, 8, 1)


def test_parse_cm_morning_uses_db_peers_to_avoid_wrong_plus_one() -> None:
    csv = "\n".join(
        [
            "ID,Date,Shift,Yield,Pen,Duration,Cow Milking Start Time",
            "100,01/08/2026,Morning,12.5,1,00:05:00,05:00:00",
        ]
    )
    reports = parse_milk_flow_report(
        csv.encode("utf-8"),
        filename="Milk Flow Report Export CM.csv",
        farm="CM",
        peer_non_morning_dates={dt.date(2026, 8, 1)},
    )
    assert len(reports) == 1
    assert reports[0].shift == "Morning"
    assert reports[0].milking_date == dt.date(2026, 8, 1)


def test_parse_gad_morning_not_bumped() -> None:
    csv = "\n".join(
        [
            "ID,Date,Shift,Yield,Pen,Duration,Cow Milking Start Time",
            "100,01/08/2026,Morning,12.5,1,00:05:00,05:00:00",
        ]
    )
    reports = parse_milk_flow_report(
        csv.encode("utf-8"),
        filename="Milk Flow Report Export GAD.csv",
        farm="GAD",
    )
    assert reports[0].milking_date == dt.date(2026, 8, 1)


def test_parse_missing_date_column_uses_fallback() -> None:
    csv = "\n".join(
        [
            "ID,Shift,Yield,Pen,Duration,Cow Milking Start Time",
            "100,Morning,12.5,1,00:05:00,05:00:00",
        ]
    )
    reports = parse_milk_flow_report(
        csv.encode("utf-8"),
        filename="Milk Flow Report Export CM.csv",
        farm="CM",
        fallback_date=dt.date(2026, 8, 1),
    )
    assert len(reports) == 1
    assert reports[0].milking_date == dt.date(2026, 8, 1)
    assert reports[0].shift == "Morning"


def test_milking_date_from_received_uses_uk_calendar_day() -> None:
    # 07:10 UTC = 08:10 BST on 1 Aug 2026
    assert milking_date_from_received("2026-08-01T07:10:00Z") == dt.date(2026, 8, 1)


def test_parse_blank_date_cells_use_fallback_without_cm_bump() -> None:
    csv = "\n".join(
        [
            "ID,Date,Shift,Yield,Pen,Duration,Cow Milking Start Time",
            "100,,Morning,12.5,1,00:05:00,05:00:00",
        ]
    )
    reports = parse_milk_flow_report(
        csv.encode("utf-8"),
        filename="Milk Flow Report Export CM.csv",
        farm="CM",
        fallback_date=dt.date(2026, 8, 1),
    )
    # Must not apply Date+1 on top of email received day.
    assert reports[0].milking_date == dt.date(2026, 8, 1)
