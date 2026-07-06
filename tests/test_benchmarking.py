"""Tests for Benchmarking forecast tables."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, BenchmarkForecastLine
from app.services.benchmarking import (
    BENCHMARK_METRIC_KEYS,
    fiscal_year_months,
    list_forecasts,
    list_metric_definitions,
    save_forecasts,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_fiscal_year_months_apr_to_mar() -> None:
    months = fiscal_year_months(2026)
    assert len(months) == 12
    assert months[0] == dt.date(2025, 4, 1)
    assert months[-1] == dt.date(2026, 3, 1)


def test_list_metric_definitions_has_thirteen_metrics() -> None:
    defs = list_metric_definitions()
    assert len(defs) == 13
    assert {d["id"] for d in defs} == set(BENCHMARK_METRIC_KEYS)


def test_list_forecasts_zero_fills_all_metrics(db: Session) -> None:
    result = list_forecasts(db, fiscal_year=2026)
    assert result["fiscal_year"] == 2026
    assert len(result["months"]) == 12
    assert set(result["metrics"].keys()) == set(BENCHMARK_METRIC_KEYS)
    cull_rows = result["metrics"]["cull"]["rows"]
    assert len(cull_rows) == 12
    assert cull_rows[0]["month_label"] == "Apr-25"
    assert cull_rows[0]["CM"]["quantity"] is None
    assert cull_rows[0]["GAD"]["unit_price"] is None


def test_save_and_reload_forecasts_round_trip(db: Session) -> None:
    save_forecasts(
        db,
        fiscal_year=2026,
        metric="cull",
        rows=[
            {
                "forecast_month": "2025-04-01",
                "farm": "CM",
                "quantity": 5,
                "unit_price": 1200.0,
            },
            {
                "forecast_month": "2025-04-01",
                "farm": "GAD",
                "quantity": 3,
                "unit_price": 1150.0,
            },
        ],
        user_id=1,
    )

    result = list_forecasts(db, fiscal_year=2026)
    april = result["metrics"]["cull"]["rows"][0]
    assert april["CM"]["quantity"] == 5
    assert april["CM"]["unit_price"] == 1200.0
    assert april["GAD"]["quantity"] == 3

    stored = db.query(BenchmarkForecastLine).all()
    assert len(stored) == 2


def test_save_clears_cells_when_both_null(db: Session) -> None:
    save_forecasts(
        db,
        fiscal_year=2026,
        metric="milk_price",
        rows=[
            {
                "forecast_month": "2025-05-01",
                "farm": "CM",
                "quantity": None,
                "unit_price": 34.5,
            },
        ],
        user_id=None,
    )
    assert db.query(BenchmarkForecastLine).count() == 1

    save_forecasts(
        db,
        fiscal_year=2026,
        metric="milk_price",
        rows=[
            {
                "forecast_month": "2025-05-01",
                "farm": "CM",
                "quantity": None,
                "unit_price": None,
            },
        ],
        user_id=None,
    )
    assert db.query(BenchmarkForecastLine).count() == 0


def test_save_unknown_metric_raises(db: Session) -> None:
    with pytest.raises(ValueError, match="Unknown metric"):
        save_forecasts(
            db,
            fiscal_year=2026,
            metric="not_a_metric",
            rows=[],
            user_id=None,
        )
