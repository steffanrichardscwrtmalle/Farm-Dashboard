"""Disease → Died should match Deaths exclusions for OFS/TB sales-deaths."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent
from app.services.events_common import (
    _build_deaths_pivot,
    _fetch_disease_event_records,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    rows = [
        ("UK1", "DIED", "NATURAL"),
        ("UK2", "DIED", "TB"),
        ("UK3", "DIED", "OFS"),
        ("UK4", "DIED", None),
    ]
    for etag, event, remark in rows:
        session.add(
            CowEvent(
                farm="CM",
                cow_id=etag[-1],
                etag=etag,
                event=event,
                event_date=dt.date(2026, 6, 15),
                month_label="Jun-26",
                remark=remark,
                lact=2,
                bdat=dt.date(2022, 1, 1),
                fdat=dt.date(2025, 1, 1),
            )
        )
    session.commit()
    yield session
    session.close()


def test_disease_died_excludes_ofs_and_tb_like_deaths(db: Session) -> None:
    farms = ["CM"]
    date_from = dt.date(2026, 6, 1)
    date_to = dt.date(2026, 6, 30)

    disease_rows = _fetch_disease_event_records(
        db,
        event_types=("DIED",),
        selected_farms=farms,
        effective_from=date_from,
        effective_to=date_to,
        selected_parity_groups=None,
        fiscal_year=None,
    )
    disease_etags = {row["etag"] for row in disease_rows}
    assert disease_etags == {"UK1", "UK4"}

    deaths_pivot = _build_deaths_pivot(
        db,
        selected_farms=farms,
        effective_from=date_from,
        effective_to=date_to,
        selected_parity_groups=None,
        fiscal_year=None,
    )
    assert deaths_pivot.get("Jun-26", {}).get("CM") == 2


def _died_lact0(
    *,
    etag: str,
    gndr: str | None,
    cbrd: int | None,
    remark: str | None = "NATURAL",
) -> CowEvent:
    return CowEvent(
        farm="CM",
        cow_id=etag[-1],
        etag=etag,
        event="DIED",
        event_date=dt.date(2026, 6, 15),
        month_label="Jun-26",
        remark=remark,
        lact=0,
        gndr=gndr,
        cbrd=cbrd,
        bdat=dt.date(2026, 1, 1),
        fdat=None,
    )


def test_disease_died_splits_youngstock_and_beef() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add_all(
        [
            _died_lact0(etag="UKD", gndr="F", cbrd=50),
            _died_lact0(etag="UKB", gndr="F", cbrd=110),
            _died_lact0(etag="UKM", gndr="M", cbrd=40),
        ]
    )
    session.commit()
    kwargs = dict(
        db=session,
        event_types=("DIED",),
        selected_farms=["CM"],
        effective_from=dt.date(2026, 6, 1),
        effective_to=dt.date(2026, 6, 30),
        fiscal_year=None,
    )
    youngstock = {
        row["etag"]
        for row in _fetch_disease_event_records(
            selected_parity_groups=["primiparous"], **kwargs
        )
    }
    beef = {
        row["etag"]
        for row in _fetch_disease_event_records(
            selected_parity_groups=["beef"], **kwargs
        )
    }
    session.close()
    assert youngstock == {"UKD"}
    assert beef == {"UKB", "UKM"}


def test_deaths_pivot_splits_youngstock_and_beef() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add_all(
        [
            _died_lact0(etag="UKD", gndr="F", cbrd=50),
            _died_lact0(etag="UKB", gndr="F", cbrd=110),
            _died_lact0(etag="UKM", gndr="M", cbrd=40),
        ]
    )
    session.commit()
    youngstock = _build_deaths_pivot(
        session,
        selected_farms=["CM"],
        effective_from=dt.date(2026, 6, 1),
        effective_to=dt.date(2026, 6, 30),
        selected_parity_groups=["primiparous"],
        fiscal_year=None,
    )
    beef = _build_deaths_pivot(
        session,
        selected_farms=["CM"],
        effective_from=dt.date(2026, 6, 1),
        effective_to=dt.date(2026, 6, 30),
        selected_parity_groups=["beef"],
        fiscal_year=None,
    )
    session.close()
    assert youngstock.get("Jun-26", {}).get("CM") == 1
    assert beef.get("Jun-26", {}).get("CM") == 2

