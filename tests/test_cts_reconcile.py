"""CTS client parse + reconcile unit tests (no live DDTS)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CtsOnHolding, HerdInventory
from app.services.cts_client import normalize_cts_etag, parse_holding_animals_xml
from app.services.cts_reconcile import reconcile_farm, replace_on_holding_snapshot

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "cts_holding_animals.xml"
)


def test_normalize_cts_etag_strips_spaces_and_zeros() -> None:
    assert normalize_cts_etag("UK 123456 789012") == "UK123456789012"
    assert normalize_cts_etag("UK000987654321") == "UK987654321"
    assert normalize_cts_etag("BE 00021428 3270") == "BE214283270"
    assert normalize_cts_etag("?") == ""
    assert normalize_cts_etag(None) == ""
    assert normalize_cts_etag("   ") == ""


def test_parse_holding_animals_xml_from_fixture() -> None:
    root = ET.parse(_FIXTURE).getroot()
    animals = parse_holding_animals_xml(root)
    assert [a.etag for a in animals] == [
        "UK987654321",
        "UK123456789012",
        "BE214283270",
    ]
    by_etag = {a.etag: a for a in animals}
    assert by_etag["UK123456789012"].breed == "HO"
    assert by_etag["UK123456789012"].sex == "F"
    assert by_etag["UK123456789012"].dob == dt.date(2020, 3, 15)
    assert by_etag["UK123456789012"].on_date == dt.date(2021, 6, 1)
    assert by_etag["UK987654321"].dob == dt.date(2019, 4, 15)


def test_reconcile_farm_buckets() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    root = ET.parse(_FIXTURE).getroot()
    animals = parse_holding_animals_xml(root)
    replace_on_holding_snapshot(
        session, farm="CM", animals=animals, sync_run_id=None
    )

    # Matched: UK123456789012 (padded form in inventory)
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK 00123456 789012",
            cow_id="101",
            gender="F",
            category="Milking",
            bdat=dt.date(2020, 3, 15),
        )
    )
    # Inventory only
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK555555555555",
            cow_id="202",
            gender="F",
            category="Youngstock",
            bdat=dt.date(2023, 1, 1),
        )
    )
    # Inventory row with blank etag ignored
    session.add(
        HerdInventory(
            farm="CM",
            etag="?",
            cow_id="303",
            gender="M",
            category="Bull",
        )
    )
    session.commit()

    result = reconcile_farm(session, "CM")
    assert result["matched_count"] == 1
    assert result["cts_only_count"] == 2  # JE + BE not in inventory
    assert result["inventory_only_count"] == 1
    assert "matched" not in result
    assert {r["etag"] for r in result["cts_only"]} == {
        "UK987654321",
        "BE214283270",
    }
    assert result["inventory_only"][0]["etag"] == "UK555555555555"
    assert result["inventory_only"][0]["cow_id"] == "202"
    assert result["inventory_only"][0]["age_days"] is not None
    assert result["inventory_only"][0]["age_months"] is not None
    assert all("age_days" in row and "age_months" in row for row in result["cts_only"])
    assert (
        session.query(CtsOnHolding).filter(CtsOnHolding.farm == "CM").count() == 3
    )
