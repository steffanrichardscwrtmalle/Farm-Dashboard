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
    FinancialForecastMappingSource,
    HerdInventory,
    StockOpeningBaseline,
)
from app.services.benchmarking import fiscal_year_months
from app.services.financial_forecast_autofill import (
    fill_financial_forecasts_from_data_sources,
    overlay_live_milk_sales_budgets,
)
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
    assert result["mappings_filled"] >= 1

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Profit & Loss|Sales"]
    heading_data = band["headings"][str(mapping.id)]
    july_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-07-01"
    )
    assert july_row["CM"] == expected_revenue
    assert july_row["GAD"] is None


def test_overlay_live_milk_sales_uses_current_price(db: Session) -> None:
    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Milk Sales",
            FinancialForecastMapping.band == "Sales",
        )
    ).first()
    assert mapping is not None
    _seed_milk_sales_inputs(db)

    milk_report = build_milk_sales_forecasts_report(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    july = next(row for row in milk_report["rows"] if row["month_start"] == "2026-07-01")
    expected = july["farms"]["CM"]["monthly_revenue"]
    assert expected is not None

    budget: dict[int, dict[str, float]] = {
        mapping.id: {"2026-07-01": 1.0},
    }
    overlay_live_milk_sales_budgets(
        db,
        farms=["CM"],
        months=fiscal_year_months(FISCAL_YEAR),
        budget_by_mapping=budget,
        today=TODAY,
    )
    assert budget[mapping.id]["2026-07-01"] == expected


def test_milk_price_change_updates_filled_milk_sales(db: Session) -> None:
    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Milk Sales",
            FinancialForecastMapping.band == "Sales",
        )
    ).first()
    assert mapping is not None
    _seed_milk_sales_inputs(db)

    fill_financial_forecasts_from_data_sources(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    first = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    july_first = next(
        row
        for row in first["bands"]["Profit & Loss|Sales"]["headings"][str(mapping.id)]["rows"]
        if row["forecast_month"] == "2026-07-01"
    )
    original = july_first["CM"]
    assert original is not None

    for line in db.scalars(
        select(BenchmarkForecastLine).where(
            BenchmarkForecastLine.fiscal_year == FISCAL_YEAR,
            BenchmarkForecastLine.metric == "milk_price",
            BenchmarkForecastLine.farm == "CM",
        )
    ).all():
        line.unit_price = 50.0
    db.commit()

    fill_financial_forecasts_from_data_sources(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    milk_report = build_milk_sales_forecasts_report(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    july = next(row for row in milk_report["rows"] if row["month_start"] == "2026-07-01")
    expected = july["farms"]["CM"]["monthly_revenue"]
    assert expected != original

    updated = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    july_updated = next(
        row
        for row in updated["bands"]["Profit & Loss|Sales"]["headings"][str(mapping.id)]["rows"]
        if row["forecast_month"] == "2026-07-01"
    )
    assert july_updated["CM"] == expected


def test_fill_milk_deductions_from_projected_litres(db: Session) -> None:
    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Milk Deductions",
            FinancialForecastMapping.band == "Purchases",
        )
    ).first()
    assert mapping is not None

    _seed_milk_sales_inputs(db)

    milk_report = build_milk_sales_forecasts_report(
        db, fiscal_year=FISCAL_YEAR, today=TODAY
    )
    july = next(row for row in milk_report["rows"] if row["month_start"] == "2026-07-01")
    litres = july["farms"]["CM"]["monthly_litres"]
    assert litres is not None
    expected_deductions = july["farms"]["CM"]["monthly_deductions"]
    assert expected_deductions == round(litres * 0.08 / 100.0)

    result = fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert result["updated"] > 0
    assert result["mappings_filled"] >= 1

    sources = [
        row.source_key
        for row in db.scalars(
            select(FinancialForecastMappingSource).where(
                FinancialForecastMappingSource.mapping_id == mapping.id
            )
        ).all()
    ]
    assert sources == ["milk_sales.monthly_deductions"]

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Profit & Loss|Purchases"]
    heading_data = band["headings"][str(mapping.id)]
    july_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-07-01"
    )
    assert july_row["CM"] == expected_deductions
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


def test_fill_rents_into_mapped_rent_heading(db: Session) -> None:
    from app.services.rental_agreements import create_rental_agreement, save_rental_payments

    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Rent",
            FinancialForecastMapping.group == "Rent",
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
        data_sources=["rents.monthly_total"],
    )
    agreement = create_rental_agreement(
        db, business="CM", farm_name="Rent Farm", farm_size=80
    )
    save_rental_payments(
        db,
        fiscal_year=FISCAL_YEAR,
        rows=[
            {
                "agreement_id": agreement["id"],
                "payment_month": "2026-04-01",
                "amount": 1250,
            }
        ],
    )

    result = fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert result["updated"] > 0
    assert result["mappings_filled"] >= 1

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Profit & Loss|Overhead Expenses"]
    heading_data = band["headings"][str(mapping.id)]
    april_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-04-01"
    )
    assert april_row["CM"] == 1250
    assert april_row["GAD"] is None


def test_fill_stock_valuation_change_into_mapped_heading(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.financial_data_sources import FINANCIAL_DATA_SOURCE_KEYS

    assert "stock_valuations.monthly_change" in FINANCIAL_DATA_SOURCE_KEYS

    mapping = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.heading == "Stock Valuation Change",
            FinancialForecastMapping.band == "Valuation Change",
        )
    ).first()
    assert mapping is not None

    sources = [
        row.source_key
        for row in db.scalars(
            select(FinancialForecastMappingSource).where(
                FinancialForecastMappingSource.mapping_id == mapping.id
            )
        ).all()
    ]
    assert sources == ["stock_valuations.monthly_change"]

    july = dt.date(2026, 7, 1)

    def fake_index(db_session, *, fiscal_year, today=None):
        assert fiscal_year == FISCAL_YEAR
        return {
            ("CM", july): 4_250.0,
            ("GAD", july): -1_100.0,
        }

    monkeypatch.setattr(
        "app.services.financial_forecast_autofill.build_stock_valuation_change_index",
        fake_index,
    )

    result = fill_financial_forecasts_from_data_sources(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert result["updated"] > 0
    assert result["mappings_filled"] >= 1

    forecast = list_financial_forecasts(db, fiscal_year=FISCAL_YEAR)
    band = forecast["bands"]["Profit & Loss|Valuation Change"]
    heading_data = band["headings"][str(mapping.id)]
    july_row = next(
        row for row in heading_data["rows"] if row["forecast_month"] == "2026-07-01"
    )
    assert july_row["CM"] == 4250
    assert july_row["GAD"] == -1100
