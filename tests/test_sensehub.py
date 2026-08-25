from __future__ import annotations

import base64
import datetime as dt

from sqlalchemy import create_engine, select
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


def test_parse_animal_list_rows_uses_cow_database_id() -> None:
    from app.services.sensehub_api import _parse_animal_list_rows

    animals, total = _parse_animal_list_rows(
        {
            "meta": {"reportName": "AnimalList", "rowTotal": 1, "rowTotalAfterFilter": 1},
            "rows": [
                {
                    "AnimalIDCalculation": "444444 - PT",
                    "CowDatabaseIDCalculation": 1910,
                    "CowGroupNameCalculation": "Suckling Calves",
                    "CowRfidOrScrTagNumberCalculation": "654321",
                }
            ],
        }
    )
    assert total == 1
    assert animals == [
        {
            "animal_id": 1910,
            "animal_name": "444444 - PT",
            "scr_tag": "654321",
        }
    ]

    empty, _total = _parse_animal_list_rows(
        {
            "meta": {"rowTotal": 1, "rowTotalAfterFilter": 1},
            "rows": [
                {
                    "AnimalIDCalculation": "111111",
                    "CowDatabaseIDCalculation": 77,
                    "CowRfidOrScrTagNumberCalculation": "",
                }
            ],
        }
    )
    assert empty == [
        {"animal_id": 77, "animal_name": "111111", "scr_tag": None}
    ]


def test_is_herd_report_matches_name_variants() -> None:
    from app.services.sensehub_api import compact_report_name, is_herd_report

    assert is_herd_report("Animals in Herd")
    assert is_herd_report("AnimalsInHerd")
    assert is_herd_report("animals in herd")
    assert compact_report_name("Animals in Herd") == compact_report_name("AnimalsInHerd")
    assert not is_herd_report("Young Stock Health by Age All")


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
        "app.services.sensehub_import.refresh_sensehub_list_snapshots",
        lambda *args, **kwargs: {"herd_saved": 0, "no_data_saved": 0},
    )
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


def _add_herd_snapshot(session: Session, animals: list[dict]) -> None:
    session.add(
        SenseHubReportSnapshot(
            report_key=900001,
            report_name="Animals in Herd",
            title="Animals in Herd",
            row_count=len(animals),
            payload={
                "rows": [
                    {
                        "AnimalID": item["animal_name"],
                        "CowDatabaseID": item.get("animal_id"),
                        "CowRfidOrScrTagNumber": item.get("scr_tag"),
                    }
                    for item in animals
                ]
            },
            fetched_at=dt.datetime(2026, 8, 25, 16, 0, 0),
        )
    )


def _add_no_data_snapshot(session: Session, animals: list[dict]) -> None:
    session.add(
        SenseHubReportSnapshot(
            report_key=900002,
            report_name="No Data",
            title="No Data",
            row_count=len(animals),
            payload={
                "rows": [
                    {
                        "AnimalID": item["animal_name"],
                        "CowDatabaseID": item["animal_id"],
                        "CowScrTagNumber": item.get("scr_tag"),
                        "AgeInDays": item.get("age_days"),
                        "DaysWithAssignedTag": item.get("days_with_assigned_tag"),
                    }
                    for item in animals
                ]
            },
            fetched_at=dt.datetime(2026, 8, 25, 16, 0, 0),
        )
    )


def test_normalize_animal_id_keeps_first_six_digits() -> None:
    from app.services.sensehub_youngstock import normalize_animal_id

    assert normalize_animal_id("435259") == "435259"
    assert normalize_animal_id("435259ABC") == "435259"
    assert normalize_animal_id("UK435259") == "435259"
    assert normalize_animal_id(" 435259 ABC ") == "435259"
    assert normalize_animal_id("535666 - PT") == "535666"
    assert normalize_animal_id("535666 - Pen 12") == "535666"


def test_scr_id_keys_ignore_sensehub_remark_digits() -> None:
    from app.services.sensehub_youngstock import _scr_id_keys

    assert _scr_id_keys("535666 - PT") == {"535666"}
    assert _scr_id_keys("535666 - Pen 12") == {"535666"}
    assert _scr_id_keys("535666 - Heifer") == {"535666"}


def test_past_slots_skips_future_uk_times() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import past_slots

    now = dt.datetime(2026, 8, 23, 7, 30, tzinfo=ZoneInfo("Europe/London"))
    slots = past_slots(1, now=now)
    names = [(sampled.isoformat(), name) for sampled, name, _unix in slots]
    assert ("2026-08-23T06:00:00", "6am") in names
    assert ("2026-08-23T12:00:00", "midday") not in names
    assert all(unix > 0 for _sampled, _name, unix in slots)


def test_reading_target_uses_live_hour_between_locked_slots() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import LIVE_SLOT, reading_target

    now = dt.datetime(2026, 8, 23, 10, 15, tzinfo=ZoneInfo("Europe/London"))
    sampled, slot = reading_target(now)
    assert slot == LIVE_SLOT
    assert sampled == dt.datetime(2026, 8, 23, 10, 0, 0)


def test_reading_target_locks_six_am_and_midday() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import reading_target

    six_am = dt.datetime(2026, 8, 23, 6, 10, tzinfo=ZoneInfo("Europe/London"))
    sampled, slot = reading_target(six_am)
    assert slot == "6am"
    assert sampled == dt.datetime(2026, 8, 23, 6, 0, 0)
    midday = dt.datetime(2026, 8, 23, 12, 5, tzinfo=ZoneInfo("Europe/London"))
    sampled, slot = reading_target(midday)
    assert slot == "midday"
    assert sampled == dt.datetime(2026, 8, 23, 12, 0, 0)


def test_save_current_reading_overwrites_live_and_keeps_locked_slot() -> None:
    from zoneinfo import ZoneInfo

    from app.services.sensehub_youngstock import LIVE_SLOT, save_current_reading, save_rows

    session = _youngstock_db()
    locked = dt.datetime(2026, 8, 23, 6, 0, 0)
    save_rows(
        session,
        [{"AnimalID": "435259", "YoungStockHealthIndex": 82}],
        sampled_at=locked,
        slot="6am",
    )
    session.commit()
    ten = dt.datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("Europe/London"))
    save_current_reading(
        session,
        [{"AnimalID": "435259", "YoungStockHealthIndex": 70}],
        when=ten,
    )
    session.commit()
    eleven = dt.datetime(2026, 8, 23, 11, 0, tzinfo=ZoneInfo("Europe/London"))
    save_current_reading(
        session,
        [{"AnimalID": "435259", "YoungStockHealthIndex": 65}],
        when=eleven,
    )
    session.commit()
    rows = list(
        session.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.animal_id == "435259")
            .order_by(SenseHubYoungstockHealth.sampled_at.asc())
        ).all()
    )
    assert [(row.slot, row.health_index) for row in rows] == [("6am", 82.0), (LIVE_SLOT, 65.0)]
    assert rows[1].sampled_at == dt.datetime(2026, 8, 23, 11, 0, 0)
    session.close()


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
    forced = slots_to_fetch(all_slots, set(), catch_up=False)
    assert [item[1] for item in forced] == ["6pm", "midday", "6am", "midnight"]
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


def _stub_untagged_animals(
    monkeypatch,
    animals: list[dict] | None = None,
    registered: list[dict] | None = None,
) -> None:
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.list_untagged_sensehub_animals",
        lambda: list(animals or []),
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.list_sensehub_animals",
        lambda: list(registered if registered is not None else animals or []),
    )


def test_list_unassigned_calves_is_pen_110_dairy_without_sensehub(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_unassigned_calves, save_rows

    _stub_untagged_animals(monkeypatch)

    session = _youngstock_db()
    sampled = dt.datetime(2026, 8, 23, 12, 0, 0)
    save_rows(
        session,
        [{"AnimalID": "435259", "YoungStockHealthIndex": 82}],
        sampled_at=sampled,
        slot="midday",
    )
    session.add_all(
        [
            HerdInventory(
                farm="CM",
                cow_id="435259",
                etag="UK123456435259",
                category="Dairy",
                pen="110",
                aged=20,
            ),
            HerdInventory(
                farm="CM",
                cow_id="111111",
                etag="UK000000111111",
                category="Dairy",
                pen="110",
                aged=12,
            ),
            HerdInventory(
                farm="CM",
                cow_id="222222",
                etag="UK000000222222",
                category="Beef",
                pen="110",
                aged=14,
            ),
            HerdInventory(
                farm="CM",
                cow_id="333333",
                etag="UK000000333333",
                category="Dairy",
                pen="111",
                aged=18,
            ),
            HerdInventory(
                farm="CM",
                cow_id="888888",
                etag="UK435259888888",
                category="Youngstock",
                pen="110",
                aged=9,
            ),
        ]
    )
    session.commit()

    dairy = list_unassigned_calves(session)
    assert [row["cow_id"] for row in dairy["animals"]] == ["888888", "111111"]
    assert dairy["animals"][0]["etag4"] == "8888"
    assert dairy["animals"][0]["reason"] == "Calf doesn't have SCR tag"
    both = list_unassigned_calves(session, categories=["Dairy", "Beef"])
    assert [row["cow_id"] for row in both["animals"]] == ["888888", "222222", "111111"]

    save_rows(
        session,
        [
            {"AnimalID": "435259", "YoungStockHealthIndex": 82},
            {"AnimalID": "999999", "YoungStockHealthIndex": 70},
        ],
        sampled_at=sampled,
        slot="midday",
    )
    session.commit()
    dairy_with_wrong = list_unassigned_calves(session)
    by_id = {row["cow_id"]: row["reason"] for row in dairy_with_wrong["animals"]}
    assert by_id["111111"] == "Calf doesn't have SCR tag"
    assert by_id["888888"] == "Calf doesn't have SCR tag"
    assert by_id["999999"] == "Calf ID probably wrong on SCR"
    assert "435259" not in by_id
    assert next(row["scr_tag"] for row in dairy_with_wrong["animals"] if row["cow_id"] == "999999") is None
    session.close()


def test_birth_date_to_epoch_is_uk_midnight() -> None:
    from app.services.sensehub_api import birth_date_to_epoch

    assert birth_date_to_epoch(dt.date(2026, 8, 1)) == 1785538800


def test_pick_suckling_calves_group_from_nested_metadata() -> None:
    from app.services.sensehub_api import pick_suckling_calves_group

    group = pick_suckling_calves_group(
        {
            "groupList": [
                {"id": 9, "number": 2, "name": "Weaned Calves"},
                {"id": 5, "number": 1, "name": "Suckling Calves"},
            ]
        }
    )
    assert group == {"id": 5, "number": 1, "name": "Suckling Calves"}


def test_save_scr_tag_creates_sensehub_calf(monkeypatch) -> None:
    from app.services.sensehub_youngstock import (
        inventory_assignment_key,
        list_unassigned_calves,
        save_scr_tag,
    )

    created: dict[str, object] = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return {"status": 201}

    _stub_untagged_animals(monkeypatch)
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.create_sensehub_calf",
        fake_create,
    )
    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            category="Dairy",
            pen="110",
            aged=12,
            bdat=dt.date(2026, 8, 1),
        )
    )
    session.commit()
    key = inventory_assignment_key("CM", "111111")
    saved = save_scr_tag(
        session,
        row_key=key,
        farm="CM",
        cow_id="111111",
        etag="UK000000111111",
        scr_tag=" 654321 ",
    )
    assert saved["scr_tag"] == "654321"
    assert saved["created_on_sensehub"] is True
    assert created["animal_name"] == "111111"
    assert created["scr_tag"] == "654321"
    assert created["birth_date"] == dt.date(2026, 8, 1)
    listing = list_unassigned_calves(session)
    by_id = {row["cow_id"]: row for row in listing["animals"]}
    assert "111111" not in by_id
    session.close()


def test_save_scr_tag_requires_dairycomp_birth_date(monkeypatch) -> None:
    from app.services.sensehub_youngstock import inventory_assignment_key, save_scr_tag

    _stub_untagged_animals(monkeypatch)
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.create_sensehub_calf",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )
    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.commit()
    try:
        save_scr_tag(
            session,
            row_key=inventory_assignment_key("CM", "111111"),
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            scr_tag="654321",
        )
        raise AssertionError("expected missing birth date to fail")
    except ValueError as exc:
        assert "birth date" in str(exc).casefold()
    session.close()


def test_list_unassigned_calves_hides_weaned_calves(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_unassigned_calves

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    session.add_all(
        [
            HerdInventory(
                farm="CM",
                cow_id="111111",
                etag="UK000000111111",
                category="Dairy",
                pen="110",
                aged=12,
            ),
            HerdInventory(
                farm="CM",
                cow_id="888888",
                etag="UK000000888888",
                category="Dairy",
                pen="110",
                aged=90,
            ),
            CowEvent(
                farm="CM",
                cow_id="888888",
                event="WEANING",
                remark="WEANED",
                event_date=dt.date(2026, 8, 1),
            ),
        ]
    )
    session.commit()
    dairy = list_unassigned_calves(session)
    assert [row["cow_id"] for row in dairy["animals"]] == ["111111"]
    session.close()


def test_list_unassigned_calves_marks_removed_scr_tag(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_unassigned_calves

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 77, "animal_name": "111111", "scr_tag": None}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.commit()
    dairy = list_unassigned_calves(session)
    assert dairy["animals"][0]["cow_id"] == "111111"
    assert dairy["animals"][0]["reason"] == "SCR Tag has been removed"
    session.close()


def test_match_inventory_ignores_sensehub_name_suffix() -> None:
    from app.services.sensehub_youngstock import _inventory_indexes, match_inventory

    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="535666",
            etag="UK000000535666",
            category="Dairy",
            pen="110",
            aged=20,
        )
    )
    session.commit()
    by_cow, by_tag = _inventory_indexes(session)
    assert match_inventory("535666 - PT", by_cow, by_tag) is not None
    assert match_inventory("535666 - PT", by_cow, by_tag).cow_id == "535666"
    assert match_inventory("535666 - Pen 12", by_cow, by_tag).cow_id == "535666"
    session.close()


def test_list_unassigned_calves_hides_calves_already_on_sensehub_with_suffix(
    monkeypatch,
) -> None:
    from app.models import SenseHubYoungstockHealth
    from app.services.sensehub_youngstock import list_unassigned_calves, save_rows

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 88, "animal_name": "535666 - PT", "scr_tag": "18117154"}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="535666",
            etag="UK000000535666",
            category="Dairy",
            pen="110",
            aged=20,
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.commit()

    listing = list_unassigned_calves(session)
    assert [row["cow_id"] for row in listing["animals"]] == ["111111"]

    save_rows(
        session,
        [{"AnimalID": "535666 - PT", "YoungStockHealthIndex": 90}],
        sampled_at=dt.datetime(2026, 8, 23, 12, 0, 0),
        slot="midday",
    )
    session.commit()
    listing = list_unassigned_calves(session)
    assert [row["cow_id"] for row in listing["animals"]] == ["111111"]
    assert not any(row["reason"] == "Calf ID probably wrong on SCR" for row in listing["animals"])

    session.add(
        SenseHubYoungstockHealth(
            animal_id="535666 - PT",
            raw_animal_id="535666 - PT",
            sampled_at=dt.datetime(2026, 8, 23, 18, 0, 0),
            slot="6pm",
            health_index=88,
        )
    )
    session.commit()
    listing = list_unassigned_calves(session)
    assert [row["cow_id"] for row in listing["animals"]] == ["111111"]
    session.close()


def test_list_unassigned_uses_report_snapshot_animal_ids(monkeypatch) -> None:
    from app.models import SenseHubReportSnapshot
    from app.services.sensehub_youngstock import list_unassigned_calves

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="535666",
            etag="UK000000535666",
            category="Dairy",
            pen="110",
            aged=20,
        )
    )
    session.add(
        SenseHubReportSnapshot(
            report_key=1,
            report_name="Young Stock Health by Age All",
            title="Young Stock Health by Age All",
            row_count=1,
            payload={"rows": [{"AnimalID": "535666 - Heifer 2"}]},
            fetched_at=dt.datetime(2026, 8, 23, 6, 0, 0),
        )
    )
    session.commit()
    listing = list_unassigned_calves(session)
    assert listing["animals"] == []
    session.close()


def test_list_unassigned_uses_animals_in_herd_without_health_data(monkeypatch) -> None:
    from app.models import SenseHubReportSnapshot
    from app.services.sensehub_youngstock import list_unassigned_calves

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="444444",
            etag="UK000000444444",
            category="Dairy",
            pen="110",
            aged=8,
        )
    )
    session.add(
        SenseHubReportSnapshot(
            report_key=99,
            report_name="Animals in Herd",
            title="Animals in Herd",
            row_count=1,
            payload={"rows": [{"AnimalID": "444444 - PT"}]},
            fetched_at=dt.datetime(2026, 8, 25, 10, 0, 0),
        )
    )
    session.commit()
    listing = list_unassigned_calves(session)
    assert listing["animals"] == []
    session.close()


def test_list_unassigned_uses_live_animals_in_herd(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_unassigned_calves

    _stub_untagged_animals(monkeypatch)
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 91, "animal_name": "444444 - PT", "scr_tag": "18117154"}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="444444",
            etag="UK000000444444",
            category="Dairy",
            pen="110",
            aged=8,
        )
    )
    session.commit()
    listing = list_unassigned_calves(session)
    assert listing["animals"] == []
    session.close()


def test_hourly_import_stores_animals_in_herd_snapshot(monkeypatch) -> None:
    from app.models import SenseHubReportSnapshot
    from app.services.sensehub_youngstock import _import_current_slot

    monkeypatch.setattr(
        "app.services.sensehub_youngstock.list_sensehub_animals",
        lambda: [{"animal_id": 9, "animal_name": "444444 - PT", "scr_tag": None}],
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.fetch_named_reports",
        lambda names: {
            "farm_id": "EU1",
            "farm_name": "Test Farm",
            "software_version": "1",
            "reports": (
                [
                    {
                        "report_key": 2,
                        "report_name": "No Data",
                        "title": "No Data",
                        "row_count": 0,
                        "columns": [],
                        "rows": [],
                    }
                ]
                if names == ["No Data"]
                else [
                    {
                        "report_key": 1,
                        "report_name": "Young Stock Health by Age All",
                        "title": "Young Stock Health by Age All",
                        "row_count": 1,
                        "columns": [],
                        "rows": [{"AnimalID": "111111", "YoungStockHealthIndex": 90}],
                    }
                ]
            ),
        },
    )
    session = _youngstock_db()
    result = _import_current_slot(session)
    assert result["saved"] == 1
    snaps = {item.report_name: item for item in session.scalars(select(SenseHubReportSnapshot)).all()}
    snap = snaps["Animals in Herd"]
    assert snap.payload["rows"][0]["AnimalID"] == "444444 - PT"
    session.close()


def test_refresh_tags_to_remove_data_stores_herd_snapshot(monkeypatch) -> None:
    from app.models import SenseHubReportSnapshot
    from app.services.sensehub_youngstock import refresh_tags_to_remove_data

    monkeypatch.setattr(
        "app.services.sensehub_youngstock.list_sensehub_animals",
        lambda: [{"animal_id": 9, "animal_name": "111111", "scr_tag": None}],
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.fetch_named_reports",
        lambda names: {"reports": []},
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.auto_cull_exited_sensehub_animals",
        lambda db: {"culled": 0},
    )
    session = _youngstock_db()
    result = refresh_tags_to_remove_data(session)
    assert result["herd_saved"] == 1
    snap = session.scalar(
        select(SenseHubReportSnapshot).where(
            SenseHubReportSnapshot.report_name == "Animals in Herd"
        )
    )
    assert snap is not None
    assert snap.payload["rows"][0]["AnimalID"] == "111111"
    session.close()


def test_save_scr_tag_assigns_when_calf_already_on_sensehub(monkeypatch) -> None:
    from app.services.sensehub_youngstock import (
        inventory_assignment_key,
        list_unassigned_calves,
        save_scr_tag,
    )

    assigned: dict[str, object] = {}

    def fake_assign(**kwargs):
        assigned.update(kwargs)
        return {"status": 201}

    _stub_untagged_animals(
        monkeypatch,
        [{"animal_id": 77, "animal_name": "111111"}],
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.assign_sensehub_monitoring_tag",
        fake_assign,
    )
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.create_sensehub_calf",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )
    session = _youngstock_db()
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            etag="UK000000111111",
            category="Dairy",
            pen="110",
            aged=12,
            bdat=dt.date(2026, 8, 1),
        )
    )
    session.commit()
    saved = save_scr_tag(
        session,
        row_key=inventory_assignment_key("CM", "111111"),
        farm="CM",
        cow_id="111111",
        etag="UK000000111111",
        scr_tag="654321",
    )
    assert saved["assigned_on_sensehub"] is True
    assert saved["created_on_sensehub"] is False
    assert assigned["animal_id"] == 77
    assert assigned["scr_tag"] == "654321"
    listing = list_unassigned_calves(session)
    assert "111111" not in {row["cow_id"] for row in listing["animals"]}
    session.close()


def test_list_tags_to_remove_is_untagged_youngstock(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [
            {"animal_id": 2101, "animal_name": "111111", "scr_tag": None},
            {"animal_id": 9, "animal_name": "1143", "scr_tag": None},
        ],
    )
    session.add_all(
        [
            HerdInventory(
                farm="CM",
                cow_id="111111",
                etag="UK000000111111",
                category="Dairy",
                pen="110",
                aged=12,
            ),
            HerdInventory(
                farm="CM",
                cow_id="1143",
                category="Dairy",
                pen="21",
                aged=1200,
            ),
        ]
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert [row["id"] for row in listing["animals"]] == ["1143", "111111"]
    by_id = {row["id"]: row for row in listing["animals"]}
    assert by_id["111111"]["age_days"] == 12
    assert by_id["111111"]["animal_id"] == 2101
    assert by_id["111111"]["scr_tag"] is None
    assert by_id["111111"]["reason"] == "No SCR tag"
    assert by_id["1143"]["reason"] == "No SCR tag"
    session.close()


def test_cull_tags_to_remove_sends_today_cull_ids(monkeypatch) -> None:
    from app.services.sensehub_youngstock import cull_tags_to_remove

    sent: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update({"ids": list(ids), "occurred_on": occurred_on}) or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 2101, "animal_name": "111111", "scr_tag": None}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.commit()
    result = cull_tags_to_remove(session)
    assert sent["ids"] == [2101]
    assert result["culled"] == 1
    session.close()


def test_cull_tags_to_remove_one_animal(monkeypatch) -> None:
    from app.services.sensehub_youngstock import cull_tags_to_remove

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update({"ids": list(ids), "occurred_on": occurred_on}) or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [
            {"animal_id": 2101, "animal_name": "111111", "scr_tag": None},
            {"animal_id": 2102, "animal_name": "222222", "scr_tag": None},
        ],
    )
    session.add_all(
        [
            HerdInventory(
                farm="CM",
                cow_id="111111",
                category="Dairy",
                pen="110",
                aged=12,
            ),
            HerdInventory(
                farm="CM",
                cow_id="222222",
                category="Dairy",
                pen="110",
                aged=14,
            ),
        ]
    )
    session.commit()
    result = cull_tags_to_remove(session, animal_id=2101)
    assert sent["ids"] == [2101]
    assert result["culled"] == 1
    session.close()


def test_list_tags_to_remove_includes_no_data_animals(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    session = _youngstock_db()
    _add_no_data_snapshot(
        session,
        [
            {
                "animal_id": 2100,
                "animal_name": "235565",
                "age_days": 17,
                "scr_tag": "12345678",
            }
        ],
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert listing["animals"][0]["id"] == "235565"
    assert listing["animals"][0]["scr_tag"] == "12345678"
    assert listing["animals"][0]["reason"] == "No Data"
    assert listing["animals"][0]["age_days"] == 17
    session.close()


def test_cull_tags_to_remove_selected_ids(monkeypatch) -> None:
    from app.services.sensehub_youngstock import cull_tags_to_remove

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update({"ids": list(ids), "occurred_on": occurred_on}) or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [
            {"animal_id": 2101, "animal_name": "111111", "scr_tag": None},
            {"animal_id": 2102, "animal_name": "222222", "scr_tag": None},
        ],
    )
    session.add_all(
        [
            HerdInventory(
                farm="CM",
                cow_id="111111",
                category="Dairy",
                pen="110",
                aged=12,
            ),
            HerdInventory(
                farm="CM",
                cow_id="222222",
                category="Dairy",
                pen="110",
                aged=14,
            ),
        ]
    )
    session.commit()
    result = cull_tags_to_remove(session, animal_ids=[2102])
    assert sent["ids"] == [2102]
    assert result["culled"] == 1
    session.close()


def test_list_tags_to_remove_auto_culls_sold_animals(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update(
            {"ids": list(ids), "occurred_on": occurred_on}
        )
        or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 2101, "animal_name": "111111", "scr_tag": None}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="111111",
            event="SOLD",
            event_date=dt.date(2026, 8, 20),
        )
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert sent["ids"] == [2101]
    assert sent["occurred_on"] == dt.date(2026, 8, 20)
    assert listing["animals"] == []
    assert listing["auto_culled"] == 1
    session.close()


def test_list_tags_to_remove_auto_culls_died_animals(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update(
            {"ids": list(ids), "occurred_on": occurred_on}
        )
        or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_herd_snapshot(
        session,
        [{"animal_id": 2101, "animal_name": "111111", "scr_tag": None}],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="111111",
            event="DIED",
            event_date=dt.date(2026, 8, 18),
        )
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert sent["ids"] == [2101]
    assert sent["occurred_on"] == dt.date(2026, 8, 18)
    assert listing["animals"] == []
    assert listing["auto_culled"] == 1
    session.close()


def test_days_with_assigned_tag_reads_custom_report_field() -> None:
    from app.services.sensehub_api import days_with_assigned_tag

    assert days_with_assigned_tag({"DaysWithAssignedTagCalculation": 2}) == 2
    assert days_with_assigned_tag({"days with assigned tag": "3"}) == 3
    assert days_with_assigned_tag({"AgeInDays": 10}) is None


def test_list_tags_to_remove_hides_recently_assigned_no_data(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    session = _youngstock_db()
    _add_no_data_snapshot(
        session,
        [
            {
                "animal_id": 2100,
                "animal_name": "235565",
                "age_days": 17,
                "scr_tag": "12345678",
                "days_with_assigned_tag": 2,
            },
            {
                "animal_id": 2102,
                "animal_name": "235566",
                "age_days": 19,
                "scr_tag": "11223344",
                "days_with_assigned_tag": 3,
            },
            {
                "animal_id": 2101,
                "animal_name": "111111",
                "age_days": 40,
                "scr_tag": "87654321",
                "days_with_assigned_tag": 8,
            },
        ],
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert [row["id"] for row in listing["animals"]] == ["111111", "235566"]
    by_id = {row["id"]: row for row in listing["animals"]}
    assert by_id["111111"]["days_with_assigned_tag"] == 8
    assert by_id["235566"]["days_with_assigned_tag"] == 3
    session.close()


def test_list_tags_to_remove_still_auto_culls_recent_sold_no_data(monkeypatch) -> None:
    from app.services.sensehub_youngstock import list_tags_to_remove

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "app.services.sensehub_youngstock.cull_sensehub_animals",
        lambda ids, occurred_on=None, **kwargs: sent.update(
            {"ids": list(ids), "occurred_on": occurred_on}
        )
        or {"culled": len(ids), "animal_ids": list(ids)},
    )
    session = _youngstock_db()
    _add_no_data_snapshot(
        session,
        [
            {
                "animal_id": 2101,
                "animal_name": "111111",
                "age_days": 20,
                "scr_tag": "12345678",
                "days_with_assigned_tag": 1,
            }
        ],
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="111111",
            category="Dairy",
            pen="110",
            aged=12,
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="111111",
            event="SOLD",
            event_date=dt.date(2026, 8, 20),
        )
    )
    session.commit()
    listing = list_tags_to_remove(session)
    assert sent["ids"] == [2101]
    assert sent["occurred_on"] == dt.date(2026, 8, 20)
    assert listing["animals"] == []
    session.close()
