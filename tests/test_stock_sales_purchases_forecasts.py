"""Tests for stock sales / purchases forecasts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, BenchmarkForecastLine
from app.services.stock_sales_purchases_forecasts import (
    _line_value,
    _sum_available,
    build_stock_sales_purchases_forecasts_report,
)

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


def test_line_value_multiplies_quantity_and_price() -> None:
    assert _line_value(5.0, 1200.0) == 6000
    assert _line_value(None, 1200.0) is None
    assert _line_value(5.0, None) is None


def test_sum_available_ignores_missing_components() -> None:
    assert _sum_available(1000, None, 500) == 1500
    assert _sum_available(None, None) is None


def test_build_stock_sales_purchases_forecasts_report(db: Session) -> None:
    month = dt.date(2026, 7, 1)
    rows = [
        ("cull", 2, 1000.0),
        ("cow_sale", 1, 1500.0),
        ("cow_purchase", 3, 800.0),
    ]
    for metric, quantity, unit_price in rows:
        db.add(
            BenchmarkForecastLine(
                fiscal_year=FISCAL_YEAR,
                forecast_month=month,
                metric=metric,
                farm="CM",
                quantity=quantity,
                unit_price=unit_price,
            )
        )
    db.commit()

    report = build_stock_sales_purchases_forecasts_report(
        db,
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )
    assert len(report["rows"]) == 12
    july = next(row for row in report["rows"] if row["month_start"] == "2026-07-01")
    assert july["source"] == "projected"
    assert july["farms"]["CM"]["sales"] == 3500
    assert july["farms"]["CM"]["purchases"] == 2400
    assert july["farms"]["CM"]["detail"]["cull"] == 2000
    assert july["farms"]["CM"]["detail"]["cow_purchase"] == 2400
