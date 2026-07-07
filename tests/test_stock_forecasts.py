"""Tests for Benchmarking stock forecasts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    BenchmarkForecastLine,
    CowEvent,
    HerdInventory,
    StockOpeningBaseline,
)
from app.services.stock_forecasts import build_stock_forecasts_report

TODAY = dt.date(2026, 7, 6)
FISCAL_YEAR = 2027


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _seed_cows_baseline(db: Session, opening: int = 100) -> None:
    db.add(
        StockOpeningBaseline(
            farm="CM",
            stock_group="cows",
            month_start=dt.date(2024, 4, 1),
            opening_count=opening,
        )
    )
    db.commit()


def _seed_youngstock_baseline(db: Session, opening: int = 200) -> None:
    db.add(
        StockOpeningBaseline(
            farm="CM",
            stock_group="youngstock",
            month_start=dt.date(2024, 4, 1),
            opening_count=opening,
        )
    )
    db.commit()


def _report(
    db: Session,
    *,
    stock_group: str = "cows",
    fiscal_year: int = FISCAL_YEAR,
) -> dict:
    return build_stock_forecasts_report(
        db,
        farms=["CM"],
        stock_group=stock_group,
        fiscal_year=fiscal_year,
        today=TODAY,
    )


def test_full_fiscal_year_runs_through_march(db: Session) -> None:
    _seed_cows_baseline(db)
    rows = _report(db)["rows"]
    assert len(rows) == 12
    assert rows[0]["month_start"] == "2026-04-01"
    assert rows[0]["source"] == "actual"
    assert rows[-1]["month_start"] == "2027-03-01"
    assert rows[-1]["source"] == "projected"
    assert rows[-1]["event_month"] == "Mar-27"


def test_actual_and_projected_split(db: Session) -> None:
    _seed_cows_baseline(db)
    result = _report(db)
    rows = result["rows"]
    assert rows
    actual = [r for r in rows if r["source"] == "actual"]
    projected = [r for r in rows if r["source"] == "projected"]
    assert actual
    assert projected
    assert actual[-1]["month_start"] == "2026-06-01"
    assert projected[0]["month_start"] == "2026-07-01"
    assert result["actual_cutoff"] == "2026-06-01"
    assert result["projected_from"] == "2026-07-01"


def test_april_opening_equals_march_closing(db: Session) -> None:
    _seed_cows_baseline(db, opening=100)
    db.add(
        CowEvent(
            farm="CM",
            cow_id="1",
            etag="UK1",
            event="SOLD",
            event_date=dt.date(2026, 3, 10),
            lact=2,
            remark="",
            bdat=dt.date(2020, 1, 1),
        )
    )
    db.commit()

    rows = build_stock_forecasts_report(
        db,
        farms=["CM"],
        stock_group="cows",
        fiscal_year=FISCAL_YEAR,
        today=dt.date(2026, 4, 15),
    )["rows"]
    april = next(r for r in rows if r["month_start"] == "2026-04-01")
    assert april["source"] == "projected"
    assert april["opening"] == 99


def test_july_opening_equals_june_closing(db: Session) -> None:
    _seed_cows_baseline(db, opening=100)
    db.add(
        CowEvent(
            farm="CM",
            cow_id="1",
            etag="UK1",
            event="SOLD",
            event_date=dt.date(2026, 6, 10),
            lact=2,
            remark="",
            bdat=dt.date(2020, 1, 1),
        )
    )
    db.commit()

    rows = _report(db)["rows"]
    june = next(r for r in rows if r["month_start"] == "2026-06-01")
    july = next(r for r in rows if r["month_start"] == "2026-07-01")
    assert june["source"] == "actual"
    assert july["source"] == "projected"
    assert july["opening"] == june["closing"]


def test_cow_forecast_mapping_on_projected_month(db: Session) -> None:
    _seed_cows_baseline(db, opening=100)
    for metric, qty in (
        ("cull", 5),
        ("cow_sale", 3),
        ("cow_death", 2),
        ("cow_purchase", 4),
    ):
        db.add(
            BenchmarkForecastLine(
                fiscal_year=FISCAL_YEAR,
                forecast_month=dt.date(2026, 7, 1),
                metric=metric,
                farm="CM",
                quantity=qty,
            )
        )
    db.commit()

    july = next(r for r in _report(db)["rows"] if r["month_start"] == "2026-07-01")
    assert july["sales"]["CULL"] == 5
    assert july["sales"]["Dairy"] == 3
    assert july["deaths"] == 2
    assert july["purchases"] == 4
    assert july["closing"] == 100 - 5 - 3 - 2 + 4


def test_current_month_calvings_sum_already_calved_and_due(db: Session) -> None:
    _seed_cows_baseline(db)
    for idx in range(10):
        db.add(
            CowEvent(
                farm="CM",
                cow_id=f"c{idx}",
                etag=f"UK{idx}",
                event="FRESH",
                event_date=dt.date(2026, 7, 2),
                lact=1,
                cbrd=1,
                gndr="F",
                bdat=dt.date(2024, 1, 1),
            )
        )
    for idx in range(70):
        db.add(
            HerdInventory(
                farm="CM",
                cow_id=f"h{idx}",
                etag=f"HE{idx}",
                category="Youngstock",
                gender="Female",
                expected_due=dt.date(2026, 7, 20),
                expected_month="Jul-26",
                sort_key=202704,
                import_timestamp=dt.datetime(2026, 7, 1, 12, 0, 0),
            )
        )
    db.commit()

    july = next(r for r in _report(db)["rows"] if r["month_start"] == "2026-07-01")
    assert july["calvings"] == 80


def test_youngstock_projected_calvings_are_negative(db: Session) -> None:
    _seed_youngstock_baseline(db)
    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 8, 1),
            metric="holstein_calves_born",
            farm="CM",
            quantity=12,
        )
    )
    for idx in range(15):
        db.add(
            HerdInventory(
                farm="CM",
                cow_id=f"y{idx}",
                etag=f"YE{idx}",
                category="Youngstock",
                gender="Female",
                expected_due=dt.date(2026, 8, 10),
                expected_month="Aug-26",
                sort_key=202705,
                import_timestamp=dt.datetime(2026, 7, 1, 12, 0, 0),
            )
        )
    db.commit()

    rows = _report(db, stock_group="youngstock")["rows"]
    august_rows = [r for r in rows if r["month_start"] == "2026-08-01"]
    assert len(august_rows) == 1
    august = august_rows[0]
    assert august["source"] == "projected"
    assert august["births"] == 12
    assert august["calvings"] == -15


def _seed_beef_baseline(db: Session, opening: int = 50) -> None:
    db.add(
        StockOpeningBaseline(
            farm="CM",
            stock_group="beef",
            month_start=dt.date(2024, 4, 1),
            opening_count=opening,
        )
    )
    db.commit()


def test_beef_full_fiscal_year(db: Session) -> None:
    _seed_beef_baseline(db)
    rows = _report(db, stock_group="beef")["rows"]
    assert len(rows) == 12
    assert rows[0]["month_start"] == "2026-04-01"
    assert rows[-1]["month_start"] == "2027-03-01"


def test_beef_forecast_sales_mapping(db: Session) -> None:
    _seed_beef_baseline(db, opening=50)
    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 7, 1),
            metric="beef_calf_birth",
            farm="CM",
            quantity=6,
        )
    )
    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 7, 1),
            metric="beef_calf_sale",
            farm="CM",
            quantity=5,
        )
    )
    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 7, 1),
            metric="beef_cattle_sale",
            farm="CM",
            quantity=3,
        )
    )
    db.commit()

    july = next(r for r in _report(db, stock_group="beef")["rows"] if r["month_start"] == "2026-07-01")
    assert july["source"] == "projected"
    assert july["sales"]["Beef"] == 8
    assert july["sales"]["CULL"] == 0
    assert july["sales"]["Dairy"] == 0
    assert july["births"] == 6
    assert july["deaths"] == 0
    assert july["purchases"] == 0
    assert july["calvings"] == 0
    assert july["closing"] == 48


def test_beef_projections_exclude_jv_animals(db: Session) -> None:
    anchor_ts = dt.datetime(2026, 6, 30, 12, 0, 0)
    db.add(
        StockOpeningBaseline(
            farm="GAD",
            stock_group="beef",
            month_start=dt.date(2024, 4, 1),
            opening_count=1,
        )
    )
    db.add(
        HerdInventory(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            bdat=dt.date(2024, 1, 1),
            lact=0,
            sbrd="Beef",
            category="Beef",
            import_timestamp=anchor_ts,
        )
    )
    db.add(
        CowEvent(
            farm="GAD",
            cow_id="500",
            etag="UK500",
            event="PATHWAY",
            event_date=dt.date(2026, 5, 15),
            lact=0,
            cbrd=121,
            gndr="M",
            bdat=dt.date(2024, 1, 1),
        )
    )
    db.commit()

    rows = build_stock_forecasts_report(
        db,
        farms=["GAD"],
        stock_group="beef",
        fiscal_year=FISCAL_YEAR,
        today=TODAY,
    )["rows"]
    june = next(r for r in rows if r["month_start"] == "2026-06-01")
    july = next(r for r in rows if r["month_start"] == "2026-07-01")
    assert june["source"] == "actual"
    assert june["closing"] == 1
    assert july["source"] == "projected"
    assert july["opening"] == 0
    assert july["closing"] == 0


def test_projected_rows_update_when_manual_forecasts_change(db: Session) -> None:
    from app.services.stock_accruals import rebuild_stock_accrual_snapshots

    _seed_cows_baseline(db, opening=100)
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
    rebuild_stock_accrual_snapshots(db)

    db.add(
        BenchmarkForecastLine(
            fiscal_year=FISCAL_YEAR,
            forecast_month=dt.date(2026, 7, 1),
            metric="cull",
            farm="CM",
            quantity=4,
        )
    )
    db.commit()

    july = next(r for r in _report(db)["rows"] if r["month_start"] == "2026-07-01")
    assert july["sales"]["CULL"] == 4

    line = db.query(BenchmarkForecastLine).one()
    line.quantity = 9
    db.commit()

    july_updated = next(r for r in _report(db)["rows"] if r["month_start"] == "2026-07-01")
    assert july_updated["sales"]["CULL"] == 9
