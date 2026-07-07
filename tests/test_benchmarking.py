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
    forecast_period_cutoff,
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


def test_list_metric_definitions_has_thirteen_tab_metrics() -> None:
    defs = list_metric_definitions()
    assert len(defs) == 13
    assert {d["id"] for d in defs} == set(BENCHMARK_METRIC_KEYS) - {"beef_calf_birth"}


def test_benchmark_metric_keys_include_beef_calf_birth() -> None:
    assert len(BENCHMARK_METRIC_KEYS) == 14
    assert "beef_calf_birth" in BENCHMARK_METRIC_KEYS


def test_list_metric_definitions_grouped_by_category() -> None:
    defs = list_metric_definitions()
    categories = [d["category"] for d in defs]
    assert categories == sorted(categories, key=lambda c: ("cow", "youngstock", "beef").index(c))
    assert categories.count("cow") == 7
    assert categories.count("youngstock") == 4
    assert categories.count("beef") == 2


def test_forecast_period_cutoff() -> None:
    cutoff = forecast_period_cutoff(today=dt.date(2026, 7, 6))
    assert cutoff["projected_from"] == "2026-07-01"
    assert cutoff["actual_cutoff"] == "2026-06-01"


def test_list_forecasts_zero_fills_all_metrics(db: Session) -> None:
    result = list_forecasts(db, fiscal_year=2026)
    assert result["fiscal_year"] == 2026
    assert len(result["months"]) == 12
    assert "projected_from" in result
    assert "actual_cutoff" in result
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


def test_beef_calf_sale_saves_births_with_sales(db: Session) -> None:
    save_forecasts(
        db,
        fiscal_year=2026,
        metric="beef_calf_sale",
        rows=[
            {
                "forecast_month": "2025-04-01",
                "farm": "CM",
                "births": 12,
                "quantity": 8,
                "unit_price": 250.0,
            },
        ],
        user_id=None,
    )

    result = list_forecasts(db, fiscal_year=2026)
    april = result["metrics"]["beef_calf_sale"]["rows"][0]
    assert april["CM"]["births"] == 12
    assert april["CM"]["quantity"] == 8
    assert april["CM"]["unit_price"] == 250.0

    stored = db.query(BenchmarkForecastLine).order_by(BenchmarkForecastLine.metric).all()
    assert len(stored) == 2
    assert stored[0].metric == "beef_calf_birth"
    assert stored[0].quantity == 12
    assert stored[1].metric == "beef_calf_sale"
    assert stored[1].quantity == 8
