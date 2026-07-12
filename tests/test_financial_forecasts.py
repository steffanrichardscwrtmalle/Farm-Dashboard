"""Tests for financial forecast mappings and monthly amounts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, FinancialForecastLine, FinancialForecastMapping
from app.services.financial_data_sources import validate_data_source_keys
from app.services.financial_forecasts import (
    DEFAULT_FINANCIAL_MAPPINGS,
    add_financial_option,
    create_financial_mapping,
    delete_financial_mapping,
    list_financial_forecasts,
    list_financial_mappings,
    save_financial_forecasts,
    seed_financial_forecasts_if_empty,
    update_financial_mapping,
    update_financial_option,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    seed_financial_forecasts_if_empty(session)
    yield session
    session.close()


def test_seed_creates_default_mappings(db: Session) -> None:
    mappings = list_financial_mappings(db)
    assert len(mappings) == len(DEFAULT_FINANCIAL_MAPPINGS)
    milk_deductions = next(
        m for m in mappings if m["heading"] == "Milk Deductions"
    )
    assert milk_deductions["data_sources"] == ["milk_sales.monthly_deductions"]
    stock_valuation_change = next(
        m for m in mappings if m["heading"] == "Stock Valuation Change"
    )
    assert stock_valuation_change["data_sources"] == [
        "stock_valuations.monthly_change"
    ]


def test_duplicate_heading_allowed_in_different_groups(db: Session) -> None:
    """Shed/ Machinery appears under HP and HP Received."""
    headings = [m["heading"] for m in list_financial_mappings(db) if m["heading"] == "Shed/ Machinery"]
    assert len(headings) == 2


def test_create_mapping_with_data_sources(db: Session) -> None:
    add_financial_option(db, "heading", "Test Heading")
    add_financial_option(db, "group", "Test Group")
    mapping = create_financial_mapping(
        db,
        heading="Test Heading",
        item_type="Profit & Loss",
        band="Sales",
        group="Test Group",
        data_sources=["milk_sales.monthly_revenue", "stock_sales_purchases.cull"],
    )
    rows = list_financial_mappings(db)
    match = next(row for row in rows if row["id"] == mapping.id)
    assert match["data_sources"] == [
        "milk_sales.monthly_revenue",
        "stock_sales_purchases.cull",
    ]


def test_validate_data_source_keys_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown data source"):
        validate_data_source_keys(["not.a.real.source"])


def test_update_mapping_keeps_same_data_source(db: Session) -> None:
    """Re-saving a mapping with an unchanged source must not hit the unique constraint."""
    add_financial_option(db, "heading", "Test Heading")
    add_financial_option(db, "group", "Test Group")
    mapping = create_financial_mapping(
        db,
        heading="Test Heading",
        item_type="Profit & Loss",
        band="Sales",
        group="Test Group",
        data_sources=["milk_sales.monthly_litres"],
    )
    update_financial_mapping(
        db,
        mapping.id,
        heading="Test Heading",
        item_type="Profit & Loss",
        band="Sales",
        group="Test Group",
        data_sources=["milk_sales.monthly_litres", "milk_sales.monthly_revenue"],
    )
    rows = list_financial_mappings(db)
    match = next(row for row in rows if row["id"] == mapping.id)
    assert match["data_sources"] == [
        "milk_sales.monthly_litres",
        "milk_sales.monthly_revenue",
    ]


def test_update_option_renames_mappings(db: Session) -> None:
    add_financial_option(db, "heading", "Old Heading")
    add_financial_option(db, "group", "Edit Group")
    option = add_financial_option(db, "heading", "Old Heading")
    mapping = create_financial_mapping(
        db,
        heading="Old Heading",
        item_type="Profit & Loss",
        band="Sales",
        group="Edit Group",
    )
    updated = update_financial_option(db, option.id, "New Heading")
    assert updated.value == "New Heading"
    db.refresh(mapping)
    assert mapping.heading == "New Heading"


def test_update_option_rejects_duplicate(db: Session) -> None:
    first = add_financial_option(db, "band", "Alpha Band")
    add_financial_option(db, "band", "Beta Band")
    with pytest.raises(ValueError, match="already exists"):
        update_financial_option(db, first.id, "Beta Band")


def test_create_update_delete_mapping(db: Session) -> None:
    add_financial_option(db, "heading", "Test Heading")
    add_financial_option(db, "group", "Test Group")
    mapping = create_financial_mapping(
        db,
        heading="Test Heading",
        item_type="Profit & Loss",
        band="Overhead Expenses",
        group="Test Group",
    )
    updated = update_financial_mapping(
        db,
        mapping.id,
        heading="Test Heading",
        item_type="Profit & Loss",
        band="Purchases",
        group="Test Group",
    )
    assert updated.band == "Purchases"
    delete_financial_mapping(db, mapping.id)
    assert db.get(FinancialForecastMapping, mapping.id) is None


def test_save_and_reload_financial_forecasts(db: Session) -> None:
    mapping = db.scalars(
        __import__("sqlalchemy").select(FinancialForecastMapping).limit(1)
    ).first()
    assert mapping is not None

    save_financial_forecasts(
        db,
        fiscal_year=2026,
        band_id=f"{mapping.item_type}|{mapping.band}",
        rows=[
            {
                "mapping_id": mapping.id,
                "forecast_month": dt.date(2025, 4, 1),
                "CM": 1200.0,
                "GAD": 800.0,
            }
        ],
        user_id=None,
    )

    result = list_financial_forecasts(db, fiscal_year=2026)
    band = result["bands"][f"{mapping.item_type}|{mapping.band}"]
    heading_data = band["headings"][str(mapping.id)]
    april = heading_data["rows"][0]
    assert april["CM"] == 1200.0
    assert april["GAD"] == 800.0


def test_clearing_amount_deletes_line(db: Session) -> None:
    mapping = db.scalars(
        __import__("sqlalchemy").select(FinancialForecastMapping).limit(1)
    ).first()
    assert mapping is not None
    band_id = f"{mapping.item_type}|{mapping.band}"

    save_financial_forecasts(
        db,
        fiscal_year=2026,
        band_id=band_id,
        rows=[
            {
                "mapping_id": mapping.id,
                "forecast_month": dt.date(2025, 5, 1),
                "CM": 500.0,
                "GAD": None,
            }
        ],
        user_id=None,
    )
    save_financial_forecasts(
        db,
        fiscal_year=2026,
        band_id=band_id,
        rows=[
            {
                "mapping_id": mapping.id,
                "forecast_month": dt.date(2025, 5, 1),
                "CM": None,
                "GAD": None,
            }
        ],
        user_id=None,
    )
    remaining = db.scalars(
        __import__("sqlalchemy").select(FinancialForecastLine).where(
            FinancialForecastLine.mapping_id == mapping.id
        )
    ).all()
    assert remaining == []
