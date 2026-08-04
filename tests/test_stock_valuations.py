"""Tests for stock valuation rules and month-end reconstruction."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, HerdBirth, HerdInventory, StockOpeningBaseline
from app.services.inventory_valuation import (
    category_from_event_proxy,
    category_from_inventory,
    compute_value,
)
from app.services.stock_accruals import build_stock_accruals_report
from app.services.stock_valuations import (
    AnimalProfile,
    EventSnapshot,
    PurchaseRecord,
    _animal_on_farm_at_close,
    _compute_transfer_stint_start,
    _effective_lact_at_close,
    _effective_transfer_start,
    _on_farm_keys,
    _resolve_state_at,
    animal_key,
    build_stock_valuations_report,
    compare_valuations_to_accruals,
    jv_beef_counts_by_farm,
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
    assert category_from_inventory(1, "HF") == "Dairy"
    assert category_from_inventory(0, "Beef") == "Beef"
    assert category_from_inventory(0, "AAX") == "Beef"
    assert category_from_inventory(0, "HEX") == "Beef"
    assert category_from_inventory(0, "Holstein") == "Youngstock"
    assert category_from_inventory(0, "HF") == "Youngstock"
    assert category_from_inventory(0, "H") == "Youngstock"


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


def test_on_farm_keys_joint_venture_not_restored_after_later_sold() -> None:
    """JV animals sold after close must not reappear via exit reconstruction."""
    anchor = dt.date(2025, 6, 30)
    beef_key = animal_key("GAD", "UK777", "777")
    jv = {beef_key: dt.date(2025, 4, 15)}
    exits = {beef_key: dt.date(2025, 6, 20)}

    may_close = dt.date(2025, 5, 31)
    keys = _on_farm_keys(may_close, anchor, set(), exits, {}, jv, profiles=None)
    assert beef_key not in keys


def test_on_farm_keys_game_after_close_date_stays_in_set() -> None:
    """GAME dated after valuation close but in jv_keys is not excluded until close reaches it."""
    anchor = dt.date(2025, 6, 24)
    beef_key = animal_key("GAD", "UK666", "666")
    inv = {beef_key}
    jv = {beef_key: dt.date(2025, 6, 28)}

    june_close = min(dt.date(2025, 6, 30), anchor)
    keys = _on_farm_keys(june_close, anchor, inv, {}, {}, jv)
    assert beef_key in keys


def test_animal_excluded_when_sold_on_close_date() -> None:
    profile = AnimalProfile(
        farm="CM",
        etag="UK1",
        cow_id="1",
        bdat=dt.date(2020, 1, 1),
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 4, 30),
                lact=3,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2020, 1, 1),
                event="SOLD",
                seq=1,
            ),
        ],
    )
    assert not _animal_on_farm_at_close(profile, dt.date(2024, 4, 30))


def test_animal_on_farm_after_repurchase() -> None:
    profile = AnimalProfile(
        farm="CM",
        etag="UK2",
        cow_id="2",
        bdat=dt.date(2019, 1, 1),
        purchases=[
            PurchaseRecord(
                edat=dt.date(2024, 6, 1),
                lact=2,
                cbrd=1,
                gndr="F",
                stock_group="cows",
            ),
        ],
        events=[
            EventSnapshot(
                event_date=dt.date(2023, 12, 1),
                lact=2,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2019, 1, 1),
                event="SOLD",
                seq=1,
            ),
        ],
    )
    assert not _animal_on_farm_at_close(profile, dt.date(2024, 4, 30))
    assert _animal_on_farm_at_close(profile, dt.date(2024, 6, 30))


def test_purchased_youngstock_stays_youngstock_until_fresh() -> None:
    """Purchased in-calf heifers (lact=1) stay youngstock until their first FRESH."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="GAD",
        etag="UK740651227123",
        cow_id="123",
        bdat=dt.date(2023, 6, 1),
        purchases=[
            PurchaseRecord(
                edat=dt.date(2024, 12, 27),
                lact=1,
                cbrd=1,
                gndr="F",
                stock_group="youngstock",
            ),
        ],
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 12, 28),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 6, 1),
                event="ILL",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close)
    assert state is not None
    assert state["stock_group"] == "youngstock"


def test_manual_fresh_heifer_override_classifies_cows_at_close() -> None:
    """Known missing FRESH lact=1 rows still promote to cows when freshened before close."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="GAD",
        etag="UK752261609397",
        cow_id="9397",
        bdat=dt.date(2022, 6, 1),
        birth_category="Dairy",
        birth_cbrd=1,
        birth_gndr="F",
        events=[
            EventSnapshot(
                event_date=dt.date(2023, 6, 19),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2022, 6, 1),
                event="BRED",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close)
    assert state is not None
    assert state["stock_group"] == "cows"
    assert state["lact"] == 1


def test_no_events_before_close_uses_first_future_fresh_lact1() -> None:
    """Heifers with first FRESH lact=1 after close were lact=0 (youngstock) at close."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="GAD",
        etag="UK752261611392",
        cow_id="1392",
        bdat=dt.date(2024, 8, 14),
        inventory_lact=1,
        inventory_sbrd="Holstein",
        in_anchor_inventory=True,
        events=[
            EventSnapshot(
                event_date=dt.date(2026, 6, 21),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 8, 14),
                event="FRESH",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "youngstock"
    assert state["lact"] == 0


def test_first_future_fresh_lact2_was_cow_at_close() -> None:
    """When first FRESH is lact=2, close lact was 1 (cow) even without pre-close events."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="GAD",
        etag="UK752261309492",
        cow_id="9492",
        bdat=dt.date(2022, 7, 10),
        inventory_lact=2,
        inventory_sbrd="Holstein",
        in_anchor_inventory=True,
        events=[
            EventSnapshot(
                event_date=dt.date(2025, 6, 7),
                lact=2,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2022, 7, 10),
                event="FRESH",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "cows"
    assert state["lact"] == 1


def test_future_fresh_not_used_when_pre_close_events_exist() -> None:
    """Animals with pre-close events keep event-based classification."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="CM",
        etag="UK723916602841",
        cow_id="841",
        bdat=dt.date(2020, 1, 1),
        inventory_lact=1,
        inventory_sbrd="Holstein",
        in_anchor_inventory=True,
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 11, 14),
                lact=3,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2020, 1, 1),
                event="FOOTRIM",
                seq=1,
            ),
            EventSnapshot(
                event_date=dt.date(2026, 6, 21),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2020, 1, 1),
                event="FRESH",
                seq=2,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "cows"


def test_fresh_on_or_before_close_uses_event_path() -> None:
    """After FRESH lact=1 on/before close, animal is a cow at later closes."""
    profile = AnimalProfile(
        farm="GAD",
        etag="UK752261711099",
        cow_id="1099",
        bdat=dt.date(2024, 5, 2),
        inventory_lact=1,
        inventory_sbrd="Holstein",
        in_anchor_inventory=True,
        events=[
            EventSnapshot(
                event_date=dt.date(2025, 5, 1),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 5, 2),
                event="FRESH",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(
        profile, dt.date(2025, 5, 31), anchor_date=dt.date(2026, 6, 23)
    )
    assert state is not None
    assert state["stock_group"] == "cows"


def test_purchased_lact_zero_stays_youngstock_until_fresh() -> None:
    """Purchased heifers (lact=0) stay youngstock until a FRESH lact=1 calving."""
    dec_close = dt.date(2024, 12, 31)
    profile = AnimalProfile(
        farm="GAD",
        etag="UK740651227067",
        cow_id="067",
        bdat=dt.date(2023, 6, 1),
        purchases=[
            PurchaseRecord(
                edat=dt.date(2024, 12, 27),
                lact=0,
                cbrd=1,
                gndr="F",
                stock_group="youngstock",
            ),
        ],
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 12, 28),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 6, 1),
                event="ILL",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close)
    assert state is not None
    assert state["stock_group"] == "youngstock"
    assert state["lact"] == 1


def test_purchased_lact_zero_promoted_after_fresh() -> None:
    profile = AnimalProfile(
        farm="GAD",
        etag="UK740651227067",
        cow_id="067",
        bdat=dt.date(2023, 6, 1),
        purchases=[
            PurchaseRecord(
                edat=dt.date(2024, 12, 27),
                lact=0,
                cbrd=1,
                gndr="F",
                stock_group="youngstock",
            ),
        ],
        events=[
            EventSnapshot(
                event_date=dt.date(2025, 3, 15),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 6, 1),
                event="FRESH",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, dt.date(2025, 3, 31))
    assert state is not None
    assert state["stock_group"] == "cows"


def test_lact_one_at_month_open_without_fresh_remains_cow() -> None:
    """Cows already lact=1 before month open stay cows without a FRESH event."""
    april_close = dt.date(2024, 4, 30)
    profile = AnimalProfile(
        farm="CM",
        etag="UK740651220291",
        cow_id="291",
        bdat=dt.date(2020, 1, 30),
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 1, 18),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2020, 1, 30),
                event="FOOTRIM",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, april_close)
    assert state is not None
    assert state["stock_group"] == "cows"


def test_ill_lact_one_without_fresh_stays_youngstock() -> None:
    """Match Stock Accruals: lact=1 from ILL alone does not promote to cows."""
    april_close = dt.date(2024, 4, 30)
    profile = AnimalProfile(
        farm="CM",
        etag="UK740651424787",
        cow_id="787",
        bdat=dt.date(2022, 3, 24),
        birth_category="Dairy",
        birth_cbrd=1,
        birth_gndr="F",
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 4, 1),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2022, 3, 24),
                event="ILL",
                seq=1,
            ),
        ],
    )
    state = _resolve_state_at(profile, april_close)
    assert state is not None
    assert state["stock_group"] == "youngstock"
    assert state["lact"] == 1


def test_cross_farm_transfer_excluded_on_cm_before_purchase() -> None:
    """CM profile for a GAD-raised animal is excluded until CM purchase."""
    profile = AnimalProfile(
        farm="CM",
        etag="UK752261610454",
        cow_id="454",
        bdat=dt.date(2023, 9, 21),
        birth_category="Dairy",
        birth_cbrd=1,
        birth_gndr="F",
        purchases=[
            PurchaseRecord(
                edat=dt.date(2025, 5, 9),
                lact=0,
                cbrd=1,
                gndr="F",
                stock_group="youngstock",
            ),
        ],
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 1, 15),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 9, 21),
                event="VACC",
                seq=1,
                farm="GAD",
            ),
            EventSnapshot(
                event_date=dt.date(2025, 5, 9),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 9, 21),
                event="MOVE",
                seq=1,
                farm="CM",
            ),
        ],
    )
    farm_events = {
        profile.etag: {
            "GAD": [profile.events[0]],
            "CM": [profile.events[1]],
        }
    }
    profile.transfer_stint_start = _compute_transfer_stint_start(
        profile, farm_events_by_etag=farm_events
    )
    assert profile.transfer_stint_start == dt.date(2025, 5, 9)

    oct_close = dt.date(2024, 10, 31)
    may_close = dt.date(2025, 5, 31)
    assert _effective_transfer_start(profile, oct_close) == dt.date(2025, 5, 9)
    assert not _animal_on_farm_at_close(profile, oct_close)
    assert _animal_on_farm_at_close(profile, may_close)


def test_gad_sold_event_does_not_end_cm_on_farm_stint() -> None:
    """Farm-scoped exits: a GAD sale must not remove a CM profile from farm."""
    profile = AnimalProfile(
        farm="CM",
        etag="UK752261610454",
        cow_id="454",
        bdat=dt.date(2023, 9, 21),
        birth_category="Dairy",
        birth_cbrd=1,
        birth_gndr="F",
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 6, 1),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 9, 21),
                event="SOLD",
                seq=1,
                farm="GAD",
            ),
        ],
    )
    assert _animal_on_farm_at_close(profile, dt.date(2024, 6, 30))


def test_fresh_calving_counts_as_cow_despite_same_day_lact_zero_event() -> None:
    """Match Stock Accruals: FRESH lact=1 on calving day even if another same-day event has lact=0."""
    may_close = dt.date(2026, 5, 31)
    profile = AnimalProfile(
        farm="CM",
        etag="UK740651530465",
        cow_id="465",
        bdat=dt.date(2024, 1, 1),
        events=[
            EventSnapshot(
                event_date=dt.date(2026, 5, 12),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 1, 1),
                event="NEWCOL",
                seq=1,
            ),
            EventSnapshot(
                event_date=dt.date(2026, 5, 12),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 1, 1),
                event="FRESH",
                seq=2,
            ),
        ],
    )
    assert _effective_lact_at_close(profile, may_close) == 1
    state = _resolve_state_at(profile, may_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "cows"


def test_fresh_calving_counts_as_cow_when_later_event_has_lact_zero() -> None:
    may_close = dt.date(2026, 5, 31)
    profile = AnimalProfile(
        farm="CM",
        etag="UK740651430471",
        cow_id="471",
        bdat=dt.date(2024, 1, 1),
        events=[
            EventSnapshot(
                event_date=dt.date(2026, 5, 30),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 1, 1),
                event="FRESH",
                seq=1,
            ),
            EventSnapshot(
                event_date=dt.date(2026, 5, 31),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 1, 1),
                event="CHECK",
                seq=2,
            ),
        ],
    )
    assert _effective_lact_at_close(profile, may_close) == 1
    state = _resolve_state_at(profile, may_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "cows"


def test_repurchased_cow_uses_purchase_not_prior_sold_event() -> None:
    """Ignore pre-sale event history after a repurchase (Stock Accruals purchase-as-cows rule)."""
    dec_close = dt.date(2025, 12, 31)
    profile = AnimalProfile(
        farm="CM",
        etag="UK740651126968",
        cow_id="968",
        bdat=dt.date(2023, 2, 18),
        events=[
            EventSnapshot(
                event_date=dt.date(2024, 12, 27),
                lact=0,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2023, 2, 18),
                event="SOLD",
                seq=1,
            ),
        ],
        purchases=[
            PurchaseRecord(
                edat=dt.date(2025, 12, 28),
                lact=1,
                cbrd=1,
                gndr="F",
                stock_group="cows",
            ),
        ],
    )
    state = _resolve_state_at(profile, dec_close, anchor_date=dt.date(2026, 6, 23))
    assert state is not None
    assert state["stock_group"] == "cows"
    assert state["lact"] == 1


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


def test_headcounts_match_accruals_except_jv_beef(db: Session) -> None:
    """Reconstruction beef count should equal accruals closing minus JV transfers."""
    anchor_ts = dt.datetime(2025, 6, 30, 12, 0, 0)
    db.add(
        StockOpeningBaseline(
            farm="GAD",
            stock_group="beef",
            month_start=dt.date(2024, 4, 1),
            opening_count=1,
        )
    )
    db.add(
        HerdInventory(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            bdat=dt.date(2024, 1, 1),
            lact=0,
            sbrd="Beef",
            category="Beef",
            import_timestamp=anchor_ts,
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            event="PATHWAY",
            event_date=dt.date(2025, 5, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.commit()
    rebuild_stock_valuation_snapshots(db)

    fiscal_year = 2026
    month = dt.date(2025, 5, 1)
    acc = build_stock_accruals_report(
        db,
        farms=["GAD"],
        stock_group="beef",
        fiscal_year=fiscal_year,
        month_from=month,
        month_to=dt.date(2025, 5, 31),
    )
    acc_beef = next(r["closing"] for r in acc["rows"] if r["month_start"] == "2025-05-01")

    val = build_stock_valuations_report(
        db,
        farms=["GAD"],
        fiscal_year=fiscal_year,
        month_from=month,
        month_to=dt.date(2025, 5, 31),
    )
    may = next(m for m in val["months"] if m["month_start"] == "2025-05-01")
    val_beef = may["totals"]["GAD"]["categories"]["Beef"]["count"]
    assert val_beef == acc_beef - 1


def test_compare_valuations_to_accruals_jv_beef(db: Session) -> None:
    anchor_ts = dt.datetime(2025, 6, 30, 12, 0, 0)
    db.add(
        StockOpeningBaseline(
            farm="GAD",
            stock_group="beef",
            month_start=dt.date(2024, 4, 1),
            opening_count=1,
        )
    )
    db.add(
        HerdInventory(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            bdat=dt.date(2024, 1, 1),
            lact=0,
            sbrd="Beef",
            category="Beef",
            import_timestamp=anchor_ts,
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            event="PATHWAY",
            event_date=dt.date(2025, 5, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.commit()
    rebuild_stock_valuation_snapshots(db)

    comparison = compare_valuations_to_accruals(
        db,
        farms=["GAD"],
        fiscal_year=2026,
        month_from=dt.date(2025, 5, 1),
        month_to=dt.date(2025, 5, 31),
    )
    beef_rows = [r for r in comparison["rows"] if r["stock_group"] == "beef"]
    assert len(beef_rows) == 1
    assert beef_rows[0]["matched"] is True
    assert comparison["mismatches"] == 0


def test_compare_valuations_to_accruals_game_then_sold(db: Session) -> None:
    """GAME then later SOLD: accruals shows sale; valuations stay excluded after JV."""
    anchor_ts = dt.datetime(2025, 6, 30, 12, 0, 0)
    db.add(
        StockOpeningBaseline(
            farm="GAD",
            stock_group="beef",
            month_start=dt.date(2024, 4, 1),
            opening_count=2,
        )
    )
    for cow_id, etag in (("501", "UK501"), ("502", "UK502")):
        db.add(
            HerdInventory(
                farm="GAD",
                cow_id=cow_id,
                etag=etag,
                bdat=dt.date(2024, 1, 1),
                lact=0,
                sbrd="Beef",
                category="Beef",
                import_timestamp=anchor_ts,
            )
        )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="501",
            etag="UK501",
            event="GAME",
            event_date=dt.date(2025, 4, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="501",
            etag="UK501",
            event="SOLD",
            event_date=dt.date(2025, 6, 20),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
            remark="CAR16",
        )
    )
    db.commit()
    rebuild_stock_valuation_snapshots(db)

    comparison = compare_valuations_to_accruals(
        db,
        farms=["GAD"],
        fiscal_year=2026,
        month_from=dt.date(2025, 5, 1),
        month_to=dt.date(2025, 6, 30),
    )
    beef_rows = [r for r in comparison["rows"] if r["stock_group"] == "beef"]
    assert len(beef_rows) == 2
    assert all(row["matched"] for row in beef_rows)
    assert comparison["mismatches"] == 0


def test_jv_beef_counts_by_farm_lightweight(db: Session) -> None:
    """Page-load JV counts must not require full herd profile rebuild."""
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            event="PATHWAY",
            event_date=dt.date(2025, 5, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="501",
            etag="UK501",
            event="GAME",
            event_date=dt.date(2025, 4, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="501",
            etag="UK501",
            event="SOLD",
            event_date=dt.date(2025, 5, 20),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.commit()

    may = jv_beef_counts_by_farm(
        db, farms=["GAD", "CM"], close_date=dt.date(2025, 5, 31)
    )
    assert may["GAD"] == 1  # UK500 still on farm; UK501 sold in May
    assert may["CM"] == 0

    april = jv_beef_counts_by_farm(
        db, farms=["GAD"], close_date=dt.date(2025, 4, 30)
    )
    assert april["GAD"] == 1  # only UK501 had JV by end of April
