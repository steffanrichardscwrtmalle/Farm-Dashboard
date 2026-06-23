"""Tests for stock valuation rules and month-end reconstruction."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, HerdBirth, HerdInventory
from app.services.inventory_valuation import (
    category_from_event_proxy,
    category_from_inventory,
    compute_value,
)
from app.services.stock_valuations import (
    _on_farm_keys,
    animal_key,
    build_stock_valuations_report,
    rebuild_stock_valuation_snapshots,
)


def test_compute_value_dairy_lact_tiers() -> None:
    assert compute_value(1, "Dairy", 100) == 2500.0
    assert compute_value(2, "Dairy", 100) == 2200.0
    assert compute_value(3, "Dairy", 100) == 1800.0


def test_compute_value_beef_and_youngstock() -> None:
    assert compute_value(0, "Beef", 100) == 290.0
    assert compute_value(0, "Youngstock", 100) == 350.0
    assert compute_value(0, "Beef", 2000) == 1800.0


def test_category_from_event_proxy() -> None:
    assert category_from_event_proxy(2, 1, "F") == "Dairy"
    assert category_from_event_proxy(0, 121, "M") == "Beef"
    assert category_from_event_proxy(0, 1, "F") == "Youngstock"


def test_category_from_inventory() -> None:
    assert category_from_inventory(1, "Holstein") == "Dairy"
    assert category_from_inventory(0, "Beef") == "Beef"
    assert category_from_inventory(0, "Holstein") == "Youngstock"


def test_on_farm_keys_reconstruction() -> None:
    anchor = dt.date(2025, 6, 30)
    inv = {animal_key("GAD", "UK1", "1"), animal_key("GAD", "UK2", "2")}
    exits = {animal_key("GAD", "UK3", "3"): dt.date(2025, 5, 15)}
    entries = {animal_key("GAD", "UK4", "4"): dt.date(2025, 5, 1)}

    april_close = dt.date(2025, 4, 30)
    keys = _on_farm_keys(april_close, anchor, inv, exits, entries, {})
    assert animal_key("GAD", "UK1", "1") in keys
    assert animal_key("GAD", "UK2", "2") in keys
    assert animal_key("GAD", "UK3", "3") in keys
    assert animal_key("GAD", "UK4", "4") not in keys


def test_on_farm_keys_joint_venture_excludes_after_transfer() -> None:
    anchor = dt.date(2025, 6, 30)
    beef_key = animal_key("GAD", "UK999", "999")
    inv = {beef_key, animal_key("GAD", "UK1", "1")}
    jv = {beef_key: dt.date(2025, 5, 15)}

    april_close = dt.date(2025, 4, 30)
    may_close = dt.date(2025, 5, 31)
    june_close = dt.date(2025, 6, 30)

    april_keys = _on_farm_keys(april_close, anchor, inv, {}, {}, jv)
    assert beef_key in april_keys

    may_keys = _on_farm_keys(may_close, anchor, inv, {}, {}, jv)
    assert beef_key not in may_keys

    june_keys = _on_farm_keys(june_close, anchor, inv, {}, {}, jv)
    assert beef_key not in june_keys


def test_on_farm_keys_joint_venture_adds_back_before_anchor() -> None:
    anchor = dt.date(2025, 6, 30)
    beef_key = animal_key("GAD", "UK888", "888")
    jv = {beef_key: dt.date(2025, 5, 20)}

    april_close = dt.date(2025, 4, 30)
    keys = _on_farm_keys(april_close, anchor, set(), {}, {}, jv)
    assert beef_key in keys


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    anchor_ts = dt.datetime(2025, 6, 30, 12, 0, 0)
    session.add(
        HerdInventory(
            farm="GAD",
            cow_id="100",
            etag="UK100",
            bdat=dt.date(2020, 1, 1),
            lact=2,
            sbrd="Holstein",
            category="Dairy",
            import_timestamp=anchor_ts,
        )
    )
    session.add(
        HerdInventory(
            farm="GAD",
            cow_id="200",
            etag="UK200",
            bdat=dt.date(2025, 5, 1),
            lact=0,
            sbrd="Holstein",
            category="Youngstock",
            import_timestamp=anchor_ts,
        )
    )
    session.add(
        CowEvent(
            farm="GAD",
            cow_id="300",
            etag="UK300",
            event="SOLD",
            event_date=dt.date(2025, 5, 15),
            lact=1,
            cbrd=1,
            gndr="F",
            bdat=dt.date(2019, 1, 1),
        )
    )
    session.add(
        HerdBirth(
            farm="GAD",
            cow_id="200",
            etag="UK200",
            bdat=dt.date(2025, 5, 1),
            cbrd=1,
            gndr="F",
            category="Dairy",
        )
    )
    session.commit()
    yield session
    session.close()


def test_build_report_only_died_and_sold_exits(db: Session) -> None:
    report = build_stock_valuations_report(db, farms=["GAD"], fiscal_year=2026)
    assert report["anchor_date"] == "2025-06-30"
    assert len(report["months"]) >= 1
    april = next(m for m in report["months"] if m["month_start"] == "2025-04-01")
    june = next(m for m in report["months"] if m["month_start"] == "2025-06-01")
    assert april["grand_total_gbp"] > june["grand_total_gbp"]


def test_build_report_dedupes_inventory_and_exit(db: Session) -> None:
    report = build_stock_valuations_report(
        db,
        farms=["GAD"],
        fiscal_year=2026,
        selected_month=dt.date(2025, 4, 1),
    )
    detail = report["selected_month"]
    assert detail is not None
    assert detail["total_animals"] == 2


def test_build_report_excludes_beef_after_pathway(db: Session) -> None:
    anchor_ts = dt.datetime(2025, 6, 30, 12, 0, 0)
    db.add(
        HerdInventory(
            farm="GAD",
            cow_id="400",
            etag="UK400",
            bdat=dt.date(2024, 6, 1),
            lact=0,
            sbrd="Beef",
            category="Beef",
            import_timestamp=anchor_ts,
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="400",
            etag="UK400",
            event="PATHWAY",
            event_date=dt.date(2025, 5, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 6, 1),
        )
    )
    db.commit()

    report = build_stock_valuations_report(db, farms=["GAD"], fiscal_year=2026)
    april = next(m for m in report["months"] if m["month_start"] == "2025-04-01")
    may = next(m for m in report["months"] if m["month_start"] == "2025-05-01")
    june = next(m for m in report["months"] if m["month_start"] == "2025-06-01")

    april_beef = april["totals"]["GAD"]["categories"]["Beef"]["count"]
    may_beef = may["totals"]["GAD"]["categories"]["Beef"]["count"]
    june_beef = june["totals"]["GAD"]["categories"]["Beef"]["count"]

    assert april_beef == 1
    assert may_beef == 0
    assert june_beef == 0


def test_build_report_month_range_filter(db: Session) -> None:
    report = build_stock_valuations_report(
        db,
        farms=["GAD"],
        fiscal_year=2026,
        month_from=dt.date(2025, 5, 1),
        month_to=dt.date(2025, 5, 31),
    )
    assert report["date_bounds"] is not None
    assert len(report["months"]) == 1
    assert report["months"][0]["month_start"] == "2025-05-01"


def test_rebuild_snapshots_served_from_table(db: Session) -> None:
    live = build_stock_valuations_report(db, farms=["GAD"], fiscal_year=2026)
    assert live.get("from_snapshot") is False

    stats = rebuild_stock_valuation_snapshots(db)
    assert stats["rows_written"] > 0

    cached = build_stock_valuations_report(db, farms=["GAD"], fiscal_year=2026)
    assert cached.get("from_snapshot") is True
    assert cached["months"] == live["months"]
