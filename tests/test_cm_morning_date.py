"""CM Morning milking_date rules and missing-Date fallback."""

from __future__ import annotations

import datetime as dt

from app.services.parlour_milk_flow_import import milking_date_from_received
from app.services.parlour_milk_flow_parse import parse_milk_flow_report


def test_parse_cm_morning_bumps_report_date() -> None:
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
    )
    assert len(reports) == 1
    assert reports[0].milking_date == dt.date(2026, 8, 2)


def test_parse_cm_morning_does_not_stick_to_previous_day_peers() -> None:
    """Sunday Morning stamped 01/08 must become 02/08 even if 01/08 Day exists."""
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
    assert reports[0].milking_date == dt.date(2026, 8, 2)


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


def test_milking_date_from_received_uses_uk_calendar_day() -> None:
    # 07:10 UTC = 08:10 BST on 1 Aug 2026
    assert milking_date_from_received("2026-08-01T07:10:00Z") == dt.date(2026, 8, 1)


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
        fallback_date=dt.date(2026, 8, 2),
    )
    assert len(reports) == 1
    assert reports[0].milking_date == dt.date(2026, 8, 2)
    assert reports[0].shift == "Morning"


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
        fallback_date=dt.date(2026, 8, 2),
    )
    # Must not apply Date+1 on top of email received day.
    assert reports[0].milking_date == dt.date(2026, 8, 2)
