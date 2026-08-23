from __future__ import annotations

import base64
import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, HerdInventory, SenseHubReportSnapshot, SenseHubYoungstockHealth
from app.services.sensehub_api import (
    _basic_auth,
    catalog_from_v5_body,
    flatten_report,
    flatten_row,
    humanize_field,
)
from app.services.sensehub_import import get_sensehub_report, import_sensehub


def test_basic_auth_matches_sensehub_web_app() -> None:
    header = _basic_auth("steff@farm.test", "EU4005774", "IL01", "secret")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    assert decoded == "steff@farm.test_EU4005774_IL01:secret"


def test_humanize_field_strips_calculation_suffix() -> None:
    assert humanize_field("AnimalIDCalculation") == "Animal ID"
    assert "DIM" in humanize_field("DIMAgeDaysCalculation")


def test_flatten_row_hides_badges_and_formats_nested_reasons() -> None:
    row = flatten_row(
        {
            "rowNumber": 0,
            "rowId": 0,
            "rowType": "DataRow",
            "AnimalIDCalculation": "435259",
            "GroupNameCalculation": "Suckling Calves",
            "HealthIndexYoungStockCalculation": "82",
            "AnimalsToInspectReasonsCalculation": {
                "mostImportantReport": "YoungStockHealth",
                "restOfReports": [],
            },
            "CowDatabaseIDCalculation": 1910,
            "AnimalBadgeCalculation": "None",
            "LastUpdatedCalculation": 1700000000,
        }
    )
    assert row["AnimalID"] == "435259"
    assert row["GroupName"] == "Suckling Calves"
    assert row["AnimalsToInspectReasons"] == "YoungStockHealth"
    assert "CowDatabaseID" not in row
    assert "AnimalBadge" not in row
    assert "rowNumber" not in row
    assert "2023-11-14" in str(row["LastUpdated"])


def test_young_stock_health_uses_dashboard_columns() -> None:
    raw = {
        "result": {
            "meta": {
                "reportId": 593665,
                "reportName": "YoungStockHealth",
                "rowCount": 1,
            },
            "rows": [
                {
                    "AnimalIDCalculation": "435259",
                    "YoungStockHealthIndexCalculation": "82",
                    "AgeInDaysCalculation": 56,
                    "DailyRuminationCalculation": 116,
                    "CowGroupNameCalculation": "Suckling Calves",
                    "LactationStatusCalculation": "Heifer",
                }
            ],
        }
    }
    flat = flatten_report(
        raw,
        catalog_item={
            "key": 593665,
            "name": "YoungStockHealth",
            "category": "YoungStock",
        },
    )
    assert flat["title"] == "Young Stock Health by Age"
    assert [col["key"] for col in flat["columns"][:5]] == [
        "AnimalID",
        "YoungStockHealthIndex",
        "AgeInDays",
        "DailyRumination",
        "CowGroupName",
    ]
    assert [col["label"] for col in flat["columns"][:5]] == [
        "Animal ID",
        "Health index",
        "Age (days)",
        "Daily rumination",
        "Group",
    ]
    assert flat["rows"][0]["AnimalID"] == "435259"
    assert flat["rows"][0]["DailyRumination"] == 116


def test_catalog_includes_custom_reports() -> None:
    items = catalog_from_v5_body(
        {
            "reports": [{"key": 1, "name": "YoungStockHealth"}],
            "customReports": [
                {"key": 801968, "name": "Young Stock Health by Age All"}
            ],
        }
    )
    names = [item["name"] for item in items]
    assert names == ["YoungStockHealth", "Young Stock Health by Age All"]
    assert items[1]["is_custom"] is True


def test_young_stock_health_all_custom_report_columns() -> None:
    raw = {
        "meta": {
            "reportId": 801968,
            "reportName": "Young Stock Health by Age All",
            "rowCount": 1,
        },
        "rows": [
            {
                "AnimalIDCalculation": "134843",
                "YoungStockHealthIndexCalculation": "100",
                "AgeInDaysCalculation": 102,
                "DailyEatingTimeCalculation": 87,
                "DailyRuminationCalculation": 359,
                "CowGroupNameCalculation": "Suckling Calves",
            }
        ],
    }
    flat = flatten_report(
        raw,
        catalog_item={
            "key": 801968,
            "name": "Young Stock Health by Age All",
            "is_custom": True,
        },
    )
    assert flat["title"] == "Young Stock Health by Age All"
    assert [col["key"] for col in flat["columns"][:6]] == [
        "AnimalID",
        "YoungStockHealthIndex",
        "AgeInDays",
        "DailyEatingTime",
        "DailyRumination",
        "CowGroupName",
    ]
    assert flat["rows"][0]["DailyEatingTime"] == 87
    assert flat["row_count"] == 1


def test_flatten_report_uses_catalog_name() -> None:
    raw = {
        "result": {
            "meta": {
                "reportId": 593669,
                "reportName": "AnimalsToInspect",
                "rowCount": 1,
            },
            "rows": [
                {
                    "AnimalIDCalculation": "1",
                    "GroupNameCalculation": "Yard",
                }
            ],
        }
    }
    flat = flatten_report(
        raw,
        catalog_item={"key": 593669, "name": "AnimalsToInspect", "category": "Health"},
    )
    assert flat["report_name"] == "AnimalsToInspect"
    assert flat["title"] == "Animals to inspect"
    assert flat["row_count"] == 1
    assert flat["rows"][0]["AnimalID"] == "1"
    assert [col["key"] for col in flat["columns"]] == ["AnimalID", "GroupName"]


def test_import_replaces_snapshots(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session: Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    monkeypatch.setattr(
        "app.services.sensehub_import.fetch_all_reports",
        lambda: {
            "farm_id": "EU4005774",
            "farm_name": "Test Farm",
            "software_version": "8.3.2.357",
            "reports": [
                {
                    "report_key": 1,
                    "report_name": "AnimalsInHeat",
                    "title": "Animals in heat",
                    "category": "Reproduction",
                    "row_count": 1,
                    "report_time": None,
                    "columns": [{"key": "AnimalID", "label": "Animal ID"}],
                    "rows": [{"AnimalID": "100"}],
                }
            ],
        },
    )
    result = import_sensehub(session)
    assert result["reports_imported"] == 1
    assert result["rows_imported"] == 1
    stored = session.query(SenseHubReportSnapshot).all()
    assert len(stored) == 1
    assert stored[0].report_name == "AnimalsInHeat"

    report = get_sensehub_report(session, name="AnimalsInHeat")
    assert report["rows"] == [{"AnimalID": "100"}]
    session.close()


def _youngstock_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_normalize_animal_id_keeps_first_six_digits() -> None:
    from app.services.sensehub_youngstock import normalize_animal_id

    assert normalize_animal_id("435259") == "435259"
    assert normalize_animal_id("435259ABC") == "435259"
    assert normalize_animal_id("UK435259") == "435259"
    assert normalize_animal_id(" 435259 ABC ") == "435259"


def test_past_slots_skips_future_uk_times() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import past_slots

    now = dt.datetime(2026, 8, 23, 7, 30, tzinfo=ZoneInfo("Europe/London"))
    slots = past_slots(1, now=now)
    names = [(sampled.isoformat(), name) for sampled, name, _unix in slots]
    assert ("2026-08-23T06:00:00", "6am") in names
    assert ("2026-08-23T12:00:00", "midday") not in names
    assert all(unix > 0 for _sampled, _name, unix in slots)


def test_slots_to_fetch_catch_up_stops_at_saved_history() -> None:
    from app.services.sensehub_youngstock import slots_to_fetch

    midnight = dt.datetime(2026, 8, 23, 0, 0, 0)
    six_am = dt.datetime(2026, 8, 23, 6, 0, 0)
    midday = dt.datetime(2026, 8, 23, 12, 0, 0)
    six_pm = dt.datetime(2026, 8, 23, 18, 0, 0)
    all_slots = [
        (midnight, "midnight", 1),
        (six_am, "6am", 2),
        (midday, "midday", 3),
        (six_pm, "6pm", 4),
    ]
    existing = {midnight, six_am}
    catch_up = slots_to_fetch(
        all_slots, existing, catch_up=True, current=six_pm
    )
    assert [item[1] for item in catch_up] == ["6pm", "midday"]
    full = slots_to_fetch(all_slots, existing, catch_up=False)
    assert [item[1] for item in full] == ["6pm", "midday"]
    caught_up = slots_to_fetch(
        all_slots, {midnight, six_am, midday, six_pm}, catch_up=True, current=six_pm
    )
    assert [item[1] for item in caught_up] == ["6pm"]


def test_backfill_span_days_uses_oldest_calf_birth() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import backfill_span_days, save_rows

    session = _youngstock_db()
    sampled = dt.datetime(2026, 8, 23, 12, 0, 0)
    save_rows(
        session,
        [
            {
                "AnimalID": "435259",
                "YoungStockHealthIndex": 82,
                "AgeInDays": 56,
            },
            {
                "AnimalID": "412300",
                "YoungStockHealthIndex": 80,
                "AgeInDays": 30,
            },
        ],
        sampled_at=sampled,
        slot="midday",
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="435259",
            etag="UK123456435259",
            bdat=dt.date(2026, 6, 28),
            aged=57,
        )
    )
    session.commit()
    now = dt.datetime(2026, 8, 23, 18, 0, tzinfo=ZoneInfo("Europe/London"))
    assert backfill_span_days(session, now=now) == 57
    session.close()


def test_health_band_uses_four_colour_thresholds() -> None:
    from app.services.sensehub_youngstock import health_band

    assert health_band(79.9) == "red"
    assert health_band(80) == "yellow"
    assert health_band(84.9) == "yellow"
    assert health_band(85) == "blue"
    assert health_band(89.9) == "blue"
    assert health_band(90) == "green"
    assert health_band(None) is None


def test_etag4_is_last_four_digits_after_trim() -> None:
    from app.services.sensehub_youngstock import etag4

    assert etag4("435259") == "5259"
    assert etag4(" UK123456435259 ") == "5259"
    assert etag4("12") == "12"


def test_treatment_counts_use_disease_episode_gap() -> None:
    from app.services.sensehub_youngstock import chart_event_markers, treatment_counts

    events = [
        CowEvent(farm="CM", cow_id="435259", event="RESP", event_date=dt.date(2026, 8, 1)),
        CowEvent(farm="CM", cow_id="435259", event="RESP", event_date=dt.date(2026, 8, 1)),
        CowEvent(farm="CM", cow_id="435259", event="RESP", event_date=dt.date(2026, 8, 3)),
        CowEvent(farm="CM", cow_id="435259", event="ILL", event_date=dt.date(2026, 8, 4)),
        CowEvent(farm="CM", cow_id="435259", event="ILL", event_date=dt.date(2026, 8, 6)),
        CowEvent(farm="CM", cow_id="435259", event="ILL", event_date=dt.date(2026, 8, 15)),
        CowEvent(farm="CM", cow_id="435259", event="SCOURS", event_date=dt.date(2026, 7, 10)),
        CowEvent(farm="CM", cow_id="435259", event="SCOURS", event_date=dt.date(2026, 7, 18)),
        CowEvent(farm="CM", cow_id="435259", event="VACC", event_date=dt.date(2026, 8, 5)),
    ]
    assert treatment_counts(events) == {
        "resp_count": 1,
        "scours_count": 2,
        "ill_count": 2,
    }
    assert [marker["letter"] for marker in chart_event_markers(events)] == [
        "S",
        "S",
        "R",
        "R",
        "I",
        "V",
        "I",
        "I",
    ]


def test_list_low_health_filters_threshold_and_joins_events() -> None:
    from app.services.sensehub_youngstock import animal_events, list_low_health, save_rows

    session = _youngstock_db()
    sampled = dt.datetime(2026, 8, 23, 12, 0, 0)
    save_rows(
        session,
        [
            {
                "AnimalID": "435259ABC",
                "YoungStockHealthIndex": 82,
                "AgeInDays": 56,
            },
            {
                "AnimalID": "335391",
                "YoungStockHealthIndex": 90,
                "AgeInDays": 40,
            },
            {
                "AnimalID": "412300XYZ",
                "YoungStockHealthIndex": 80,
                "AgeInDays": 30,
            },
        ],
        sampled_at=sampled,
        slot="midday",
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="435259",
            etag=" UK123456435259 ",
            bdat=dt.date(2026, 6, 28),
            aged=57,
            pen="Calves",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="SCOURS",
            event_date=dt.date(2026, 7, 10),
            remark="Scours",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="SCOURS",
            event_date=dt.date(2026, 7, 18),
            remark="Scours",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="RESP",
            event_date=dt.date(2026, 8, 1),
            remark="Pneumonia",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="RESP",
            event_date=dt.date(2026, 8, 3),
            remark="Pneumonia",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="VACC",
            event_date=dt.date(2026, 8, 5),
            remark="BRSV",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="ILL",
            event_date=dt.date(2026, 8, 8),
            remark="Off colour",
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="435259",
            event="ILL",
            event_date=dt.date(2026, 8, 10),
            remark="Off colour",
        )
    )
    session.commit()

    listing = list_low_health(session, threshold=86)
    assert [row["animal_id"] for row in listing["animals"]] == ["412300", "435259"]
    assert listing["animals"][0]["etag4"] == "2300"
    assert listing["animals"][1]["etag4"] == "5259"
    assert listing["animals"][1]["health_index"] == 82
    assert listing["animals"][1]["age_days"] == 57
    assert listing["animals"][0]["has_dairycomp"] is False
    assert listing["animals"][1]["has_dairycomp"] is True
    assert listing["animals"][0]["resp_count"] == 0
    assert listing["animals"][0]["days_since_last_treatment"] is None
    assert listing["animals"][1]["resp_count"] == 1
    assert listing["animals"][1]["days_since_last_treatment"] == (
        dt.date.today() - dt.date(2026, 8, 10)
    ).days
    assert listing["animals"][1]["etag"] == "UK123456435259"
    assert len(listing["animals"][1]["trend"]) == 12
    assert listing["animals"][1]["trend"][-1]["band"] == "yellow"
    assert listing["animals"][1]["trend"][-1]["health_index"] == 82

    detail = animal_events(session, "435259ABC")
    assert detail["animal_id"] == "435259"
    assert detail["events"][0]["event"] == "ILL"
    assert detail["events"][0]["event_date"] == "2026-08-10"
    assert detail["resp_count"] == 1
    assert detail["scours_count"] == 2
    assert detail["ill_count"] == 1
    assert [marker["letter"] for marker in detail["chart_markers"]] == [
        "S",
        "S",
        "R",
        "R",
        "V",
        "I",
        "I",
    ]
    assert [point["sampled_at"][:10] for point in detail["health_history"]] == [
        "2026-07-10",
        "2026-07-18",
        "2026-08-01",
        "2026-08-03",
        "2026-08-05",
        "2026-08-08",
        "2026-08-10",
        "2026-08-23",
    ]
    assert detail["health_history"][-1]["health_index"] == 82
    session.close()
