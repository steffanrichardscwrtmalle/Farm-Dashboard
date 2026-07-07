"""Tests for milk sales forecasts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    BenchmarkForecastLine,
    HerdInventory,
    StockOpeningBaseline,
)
from app.services.milk_sales_forecasts import (
    _compute_milk_litres,
    _fiscal_year_days,
    build_milk_sales_forecasts_report,
)
from app.services.benchmarking import fiscal_year_months

FISCAL_YEAR = 2027
TODAY = dt.date(2026, 7, 6)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_compute_milk_litres_formula() -> None:
    fy_days = 365
    monthly, daily = _compute_milk_litres(
        avg_cows=100.0,
        average_yield=8000.0,
        month_days=30,
        fiscal_year_days=fy_days,
    )
    expected_daily = 100.0 * (8000.0 / fy_days)
    assert daily == round(expected_daily)
    assert monthly == round(expected_daily * 30)


def test_compute_milk_litres_null_without_yield() -> None:
    monthly, daily = _compute_milk_litres(
        avg_cows=100.0,
        average_yield=None,
        month_days=30,
        fiscal_year_days=365,
    )
    assert monthly is None
    assert daily is None


def _seed_baselines(db: Session) -> None:
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
    db.commit()


def test_build_milk_sales_forecasts_report_totals(db: Session) -> None:
    _seed_baselines(db)
    months = fiscal_year_months(FISCAL_YEAR)
    fy_days = _fiscal_year_days(months)
    for month_start in months:
        db.add(
            BenchmarkForecastLine(
                fiscal_year=FISCAL_YEAR,
                forecast_month=month_start,
                metric="milk_yield",
                farm="CM",
                quantity=8000.0,
            )
        )
    db.commit()

    report = build_milk_sales_forecasts_report(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert len(report["rows"]) == 12
    assert report["fiscal_year_days"] == fy_days

    july = next(row for row in report["rows"] if row["month_start"] == "2026-07-01")
    assert july["source"] == "projected"
    assert july["farms"]["CM"]["monthly_litres"] is not None
    assert july["farms"]["CM"]["daily_litres"] is not None

    cm_total = report["totals"]["CM"]["monthly_litres"]
    cm_avg_daily = report["totals"]["CM"]["daily_litres"]
    assert cm_total is not None
    assert cm_avg_daily == round(cm_total / fy_days)

    july_total = july["farms"]["Total"]
    assert july_total["monthly_litres"] == july["farms"]["CM"]["monthly_litres"]
    assert report["totals"]["Total"]["monthly_litres"] == cm_total
