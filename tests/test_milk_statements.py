"""Milk statement fiscal-year grid, including last-year comparison fields."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, MilkStatement
from app.services.milk_statements import list_milk_statements


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add(
    db: Session,
    *,
    farm: str,
    month: dt.date,
    litres: int,
    price: float,
    fat: float,
    protein: float,
) -> None:
    db.add(
        MilkStatement(
            farm=farm,
            statement_month=month,
            litres_sold=litres,
            milk_price_ppl=price,
            butterfat_pct=fat,
            protein_pct=protein,
            imported_at=dt.datetime(2026, 1, 1),
        )
    )


def test_list_attaches_prior_year_litres_price_and_composition() -> None:
    db = _session()
    _add(
        db,
        farm="CM",
        month=dt.date(2025, 4, 1),
        litres=100_000,
        price=32.156,
        fat=4.20,
        protein=3.40,
    )
    _add(
        db,
        farm="CM",
        month=dt.date(2024, 4, 1),
        litres=90_000,
        price=30.40,
        fat=4.10,
        protein=3.50,
    )
    db.commit()

    data = list_milk_statements(db, fiscal_year=2026, farms=["CM"])
    april = next(row for row in data["rows"] if row["statement_month"] == "2025-04-01")
    assert april["litres_sold"] == 100000
    assert april["prior_litres_sold"] == 90000
    assert april["milk_price_ppl"] == 32.16
    assert april["prior_milk_price_ppl"] == 30.4
    assert april["butterfat_pct"] == 4.2
    assert april["prior_butterfat_pct"] == 4.1
    assert april["protein_pct"] == 3.4
    assert april["prior_protein_pct"] == 3.5

    total = data["total"]
    assert total["prior_litres_sold"] == 90000
    assert total["prior_milk_price_ppl"] == 30.4


def test_missing_prior_year_is_blank() -> None:
    db = _session()
    _add(
        db,
        farm="GAD",
        month=dt.date(2025, 5, 1),
        litres=50_000,
        price=31.0,
        fat=4.0,
        protein=3.3,
    )
    db.commit()

    data = list_milk_statements(db, fiscal_year=2026, farms=["GAD"])
    may = next(row for row in data["rows"] if row["statement_month"] == "2025-05-01")
    assert may["litres_sold"] == 50000
    assert may["prior_litres_sold"] is None
    assert may["prior_milk_price_ppl"] is None
    assert may["prior_butterfat_pct"] is None
    assert may["prior_protein_pct"] is None
