"""Beef inventory report with JV filters."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CowEvent, HerdInventory
from app.services.beef_inventory import (
    get_beef_inventory_report,
    normalize_jv_mode,
)


def _add_beef(
    session,
    *,
    farm: str,
    etag: str,
    cow_id: str,
    months_old: int,
) -> None:
    session.add(
        HerdInventory(
            farm=farm,
            etag=etag,
            cow_id=cow_id,
            category="Beef",
            gender="Male",
            months_old=months_old,
            aged=months_old * 30,
            sbrd="AA",
            lact=0,
        )
    )


def _add_jv_event(session, *, farm: str, etag: str, cow_id: str) -> None:
    session.add(
        CowEvent(
            farm=farm,
            etag=etag,
            cow_id=cow_id,
            event="GAME",
            event_date=dt.date(2026, 1, 15),
        )
    )


def test_normalize_jv_mode() -> None:
    assert normalize_jv_mode(None) == "all"
    assert normalize_jv_mode("exclude") == "exclude"
    assert normalize_jv_mode("ONLY") == "only"
    with pytest.raises(ValueError):
        normalize_jv_mode("maybe")


def test_beef_inventory_jv_modes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    _add_beef(session, farm="CM", etag="UK1", cow_id="1", months_old=4)
    _add_beef(session, farm="CM", etag="UK2", cow_id="2", months_old=4)
    _add_beef(session, farm="GAD", etag="UK3", cow_id="3", months_old=6)
    _add_jv_event(session, farm="CM", etag="UK1", cow_id="1")
    session.commit()

    all_report = get_beef_inventory_report(session, farms=["CM", "GAD"], jv_mode="all")
    assert all_report["grand_total"]["total"] == 3
    assert all_report["grand_total"]["CM"] == 2
    assert all_report["grand_total"]["GAD"] == 1

    no_jv = get_beef_inventory_report(session, farms=["CM", "GAD"], jv_mode="exclude")
    assert no_jv["grand_total"]["total"] == 2
    assert no_jv["grand_total"]["CM"] == 1
    assert no_jv["jv_label"] == "No JV"

    only_jv = get_beef_inventory_report(session, farms=["CM", "GAD"], jv_mode="only")
    assert only_jv["grand_total"]["total"] == 1
    assert only_jv["grand_total"]["CM"] == 1
    assert only_jv["grand_total"]["GAD"] == 0
    assert only_jv["jv_label"] == "JV only"

    # Youngstock must not appear.
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK99",
            cow_id="99",
            category="Youngstock",
            gender="Female",
            months_old=4,
            aged=120,
            sbrd="HF",
            lact=0,
        )
    )
    session.commit()
    again = get_beef_inventory_report(session, farms=["CM"], jv_mode="all")
    assert again["grand_total"]["CM"] == 2
