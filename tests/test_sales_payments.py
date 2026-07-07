"""Tests for sales payments Office Admin workflow."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CattleSaleLine, CowEvent, User
from app.services.events_common import SALES_TABLE_REASON_ORDER
from app.services.sales_payments import list_sales_payments, normalize_sales_reasons


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(
        CowEvent(
            farm="CM",
            cow_id="3001",
            etag="UK740651125211",
            event="SOLD",
            event_date=dt.date(2026, 6, 4),
            dest="EUROFARM",
            remark=None,
            gndr="F",
            bdat=dt.date(2022, 1, 1),
        )
    )
    session.add(
        CowEvent(
            farm="CM",
            cow_id="3002",
            etag="UK740651329749",
            event="SOLD",
            event_date=dt.date(2026, 6, 10),
            dest="EUROFARM",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2021, 6, 1),
        )
    )
    session.add(
        CattleSaleLine(
            farm="CM",
            etag="UK740651125211",
            sale_date=dt.date(2026, 6, 5),
            cold_weight_kg=402.0,
            reject_kg=0.0,
            amount_gbp=1234.56,
        )
    )
    session.commit()
    yield session
    session.close()


def test_normalize_sales_reasons_all_selected_includes_beef() -> None:
    selected = normalize_sales_reasons(["CULL", "TB", "OFS", "Beef", "Dairy"])
    assert selected == list(SALES_TABLE_REASON_ORDER)


def test_normalize_sales_reasons_beef_only() -> None:
    assert normalize_sales_reasons(["Beef"]) == ["Beef"]


def test_normalize_sales_reasons_empty_defaults_to_all() -> None:
    assert normalize_sales_reasons(None) == list(SALES_TABLE_REASON_ORDER)


def test_list_sales_payments_includes_matched_cattle_sale_amount(db: Session) -> None:
    result = list_sales_payments(db, farms=["CM"])
    by_etag = {row["etag"]: row for row in result["rows"]}
    assert by_etag["UK740651125211"]["amount_gbp"] == 1234.56
    assert by_etag["UK740651329749"]["amount_gbp"] is None


def test_list_sales_payments_filters_tb_remarks(db: Session) -> None:
    db.add(
        CowEvent(
            farm="CM",
            cow_id="3003",
            etag="UK740651TB001",
            event="SOLD",
            event_date=dt.date(2026, 6, 12),
            dest="MARKET",
            remark="TB",
            gndr="F",
            bdat=dt.date(2020, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="CM",
            cow_id="3004",
            etag="UK740651TB002",
            event="SOLD",
            event_date=dt.date(2026, 6, 13),
            dest="MARKET",
            remark="CAR11",
            gndr="F",
            bdat=dt.date(2020, 2, 1),
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM"], reasons=["TB"])
    etags = {row["etag"] for row in result["rows"]}
    assert etags == {"UK740651TB001", "UK740651TB002"}


def test_list_sales_payments_has_amount_filter(db: Session) -> None:
    result = list_sales_payments(db, farms=["CM"], has_amount=True)
    assert result["total"] == 1
    assert result["rows"][0]["etag"] == "UK740651125211"


def test_list_sales_payments_includes_rejected_sale(db: Session) -> None:
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="210100",
            etag="UK752261210100",
            event="SOLD",
            event_date=dt.date(2026, 6, 4),
            dest="EUROFARM",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2023, 3, 20),
        )
    )
    db.add(
        CattleSaleLine(
            farm="GAD",
            etag="UK752261210100",
            sale_date=dt.date(2026, 6, 5),
            cold_weight_kg=287.5,
            reject_kg=287.5,
            amount_gbp=0.0,
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["GAD"], has_amount=True)
    row = next(r for r in result["rows"] if r["etag"] == "UK752261210100")
    assert row["sale_rejected"] is True
    assert row["has_sale_amount"] is True
    assert row["amount_gbp"] == 0.0
