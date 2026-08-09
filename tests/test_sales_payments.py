"""Tests for sales payments Office Admin workflow."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CattleSaleLine, CowEvent, SalesPaymentRecord, User
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


def test_list_sales_payments_matches_foreign_etag_zero_padding(db: Session) -> None:
    # DairyComp keeps leading zeros after country letters; Eurofarm drops them.
    db.add(
        CowEvent(
            farm="CM",
            cow_id="214283270",
            etag="BE000214283270",
            event="SOLD",
            event_date=dt.date(2026, 6, 8),
            dest="EUROFARM",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2024, 1, 15),
        )
    )
    db.add(
        CattleSaleLine(
            farm="CM",
            etag="BE214283270",
            sale_date=dt.date(2026, 6, 9),
            cold_weight_kg=310.0,
            reject_kg=0.0,
            amount_gbp=888.0,
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM"])
    row = next(r for r in result["rows"] if r["etag"] == "BE000214283270")
    assert row["amount_gbp"] == 888.0
    assert row["has_sale_amount"] is True


def test_list_sales_payments_matches_pathway_calf_amount(db: Session) -> None:
    db.add(
        CowEvent(
            farm="CM",
            cow_id="135074",
            etag="UK740651135074",
            event="SOLD",
            event_date=dt.date(2026, 6, 29),
            dest="PATHWAY",
            gndr="M",
            bdat=dt.date(2026, 5, 1),
        )
    )
    db.add(
        CattleSaleLine(
            farm="CM",
            etag="UK740651135074",
            sale_date=dt.date(2026, 6, 29),
            kill_date=dt.date(2026, 6, 29),
            cold_weight_kg=64.0,
            amount_gbp=460.0,
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM"], has_amount=True)
    row = next(r for r in result["rows"] if r["etag"] == "UK740651135074")
    assert row["amount_gbp"] == 460.0
    assert row["has_sale_amount"] is True


def test_list_sales_payments_matches_buitelaar_calf_amount(db: Session) -> None:
    db.add(
        CowEvent(
            farm="CM",
            cow_id="135200",
            etag="UK740651135200",
            event="SOLD",
            event_date=dt.date(2026, 7, 23),
            dest="BUITELAAR",
            gndr="M",
            bdat=dt.date(2026, 6, 21),
        )
    )
    db.add(
        CattleSaleLine(
            farm="CM",
            etag="UK740651135200",
            sale_date=dt.date(2026, 7, 23),
            kill_date=dt.date(2026, 7, 23),
            cold_weight_kg=56.0,
            amount_gbp=365.0,
            buyer="Buitelaar",
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM"], has_amount=True)
    row = next(r for r in result["rows"] if r["etag"] == "UK740651135200")
    assert row["amount_gbp"] == 365.0
    assert row["has_sale_amount"] is True


def test_list_sales_payments_includes_game_event(db: Session) -> None:
    """GAME JV exits appear on the sales payments queue with DEST=GAME."""
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="210200",
            etag="UK752261210200",
            event="GAME",
            event_date=dt.date(2026, 5, 15),
            dest="GAMECHANGER",
            gndr="M",
            bdat=dt.date(2024, 4, 1),
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["GAD"])
    etags = {row["etag"] for row in result["rows"]}
    assert "UK752261210200" in etags
    row = next(r for r in result["rows"] if r["etag"] == "UK752261210200")
    assert row["event_date"] == "2026-05-15"
    assert row["dest"] == "GAME"


def test_list_sales_payments_includes_path_event(db: Session) -> None:
    """PATH JV exits appear on the sales payments queue with DEST=PATH."""
    db.add(
        CowEvent(
            farm="CM",
            cow_id="135300",
            etag="UK740651135300",
            event="PATH",
            event_date=dt.date(2026, 5, 20),
            dest="PATHWAY",
            gndr="M",
            bdat=dt.date(2026, 3, 1),
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM"])
    etags = {row["etag"] for row in result["rows"]}
    assert "UK740651135300" in etags
    row = next(r for r in result["rows"] if r["etag"] == "UK740651135300")
    assert row["dest"] == "PATH"


def test_list_sales_payments_jv_exit_dest_is_event_name(db: Session) -> None:
    """GAME/PATH/PATHWAY payment rows use the event name as DEST; SOLD keeps event.dest."""
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="210400",
            etag="UK752261210400",
            event="GAME",
            event_date=dt.date(2026, 5, 1),
            dest=None,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="CM",
            cow_id="135400",
            etag="UK740651135400",
            event="PATHWAY",
            event_date=dt.date(2026, 5, 2),
            dest="SOMETHING_ELSE",
            gndr="M",
            bdat=dt.date(2025, 1, 1),
        )
    )
    db.commit()

    result = list_sales_payments(db, farms=["CM", "GAD"])
    by_etag = {row["etag"]: row for row in result["rows"]}
    assert by_etag["UK752261210400"]["dest"] == "GAME"
    assert by_etag["UK740651135400"]["dest"] == "PATHWAY"
    # Fixture SOLD rows keep CowEvent.dest unchanged
    assert by_etag["UK740651125211"]["dest"] == "EUROFARM"


def test_list_sales_payments_later_sold_after_game_appears_again(db: Session) -> None:
    """GAME then later SOLD: each exit is a separate pending payment row.

    Archiving the GAME payment must not suppress the subsequent SOLD.
    """
    etag = "UK752261210300"
    game_date = dt.date(2026, 4, 10)
    sold_date = dt.date(2026, 8, 1)
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="210300",
            etag=etag,
            event="GAME",
            event_date=game_date,
            dest="GAMECHANGER",
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="210300",
            etag=etag,
            event="SOLD",
            event_date=sold_date,
            dest="EUROFARM",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.add(
        SalesPaymentRecord(
            farm="GAD",
            cow_id="210300",
            etag=etag,
            event_date=game_date,
            paid_at=dt.datetime(2026, 4, 20),
            archived_at=dt.datetime(2026, 4, 20),
        )
    )
    db.commit()

    active = list_sales_payments(db, farms=["GAD"], status="active")
    active_dates = {
        row["event_date"] for row in active["rows"] if row["etag"] == etag
    }
    assert active_dates == {"2026-08-01"}

    archived = list_sales_payments(db, farms=["GAD"], status="archived")
    archived_dates = {
        row["event_date"] for row in archived["rows"] if row["etag"] == etag
    }
    assert archived_dates == {"2026-04-10"}
