"""Tests for filling financial forecasts from mapped data sources."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    BenchmarkForecastLine,
    FinancialForecastMapping,
    HerdInventory,
    StockOpeningBaseline,
)
from app.services.benchmarking import fiscal_year_months
from app.services.financial_forecast_autofill import fill_financial_forecasts_from_data_sources
from app.services.financial_forecasts import (
    list_financial_forecasts,
    seed_financial_forecasts_if_empty,
    update_financial_mapping,
)
from app.services.milk_sales_forecasts import build_milk_sales_forecasts_report

FISCAL_YEAR = 2027
TODAY = dt.date(2026, 7, 6)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    seed_financial_forecasts_if_empty(session)
    yield session
    session.close()


def _seed_milk_sales_inputs(db: Session) -> None:
    for farm, group, opening in [
        ("CM", "cows", 100),
        ("CM", "youngstock", 80),
        ("GAD", "cows", 90),
        ("GAD", "youngstock", 70),
    ]:
        db.add(
            StockOpeningBaseline(
                farm=farm,
                stock_group=group,
                month_start=dt.date(2024, 4, 1),
                opening_count=opening,
            )
        )
    db.add(
        HerdInventory(
            farm="CM",
            cow_id="1",
            etag="UK1",
            bdat=dt.date(2020, 1, 1),
            lact=2,
            import_timestamp=dt.datetime(2026, 6, 30, 12, 0, 0),
        )
    )
    for month_start in fiscal_year_months(FISCAL_YEAR):
        db.add(
            BenchmarkForecastLine(
                fiscal_year=FISCAL_YEAR,
                forecast_month=month_start,
                metric="milk_yield",
                farm="CM",
                quantity=8000.0,
            )
        )
        db.add(
            BenchmarkForecastLine(
                fiscal_year=FISCAL_YEAR,
                forecast_month=month_start,
                metric="milk_price",
                farm="CM",
                unit_price=40.0,
            )
        )
    db.commit()


def test_fill_milk_sales_revenue_into_mapped_heading(db: Session) -> None:
    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Milk Sales",
            FinancialForecastMapping.band == "Sales",
        )
    ).first()
    assert mapping is not None

    update_financial_mapping(
        db,
        mapping.id,
        heading=mapping.heading,
        item_type=mapping.item_type,
        band=mapping.band,
        group=mapping.group,
        data_sources=["milk_sales.monthly_revenue"],
    )
    _seed_milk_sales_inputs(db)

    milk_report = build_milk_sales_forecasts_report(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    july = next(row for row in milk_report["rows"] if row["month_start"] == "2026-07-01")
    expected_revenue = july["farms"]["CM"]["monthly_revenue"]
    assert expected_revenue is not None

    result = fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert result["updated"] > 0
    assert result["mappings_filled"] == 1

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Profit & Loss|Sales"]
    heading_data = band["headings"][str(mapping.id)]
    july_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-07-01"
    )
    assert july_row["CM"] == expected_revenue
    assert july_row["GAD"] is None


def test_fill_hp_schedule_capital_into_mapped_heading(db: Session) -> None:
    from app.services.hp_schedules import create_hp_schedule

    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Budget Capital Repayment HP",
        )
    ).first()
    assert mapping is not None

    update_financial_mapping(
        db,
        mapping.id,
        heading=mapping.heading,
        item_type=mapping.item_type,
        band=mapping.band,
        group=mapping.group,
        data_sources=["hp_schedules.monthly_capital"],
    )
    create_hp_schedule(
        db,
        name="Test Tractor",
        business="CM",
        description="HP autofill",
        monthly_capital=1000,
        monthly_interest=100,
        months=12,
        payment_day=18,
        start_month="2026-04",
    )

    result = fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert result["updated"] > 0
    assert result["mappings_filled"] == 1

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Cash|Current Liabilities"]
    heading_data = band["headings"][str(mapping.id)]
    april_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-04-01"
    )
    assert april_row["CM"] == 1000
    assert april_row["GAD"] is None
