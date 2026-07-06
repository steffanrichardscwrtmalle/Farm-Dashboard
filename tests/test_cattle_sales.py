"""Tests for cattle sales PDF parsing and event linkage helpers."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CattleSaleLine, CowEvent
from app.services.cattle_sale_pdf import (
    _parse_table_rows,
    is_acceptable_sale_line,
    is_plausible_carcass_row,
    is_rejected_sale,
    normalize_etag,
    parse_cattle_sale_pdf,
)
from app.services.cattle_sales import (
    compute_dim_at_cull,
    compute_price_per_kg,
    format_age_years_months,
    list_cattle_sales,
)


SAMPLE_TEXT = """
Eurofarm Wales Ltd
Cheque Payment Report
Payment Date: 15/03/2026

Ear Tag    Cold Weight    Amount
UK752261609397    350.50    1,234.56
UK752261509354    280.00    980.00
"""


def test_normalize_etag_adds_uk_prefix():
    assert normalize_etag("752261609397") == "UK752261609397"
    assert normalize_etag("UK752261609397") == "UK752261609397"


def test_format_age_years_months():
    assert format_age_years_months(800) == "2y 2m"
    assert format_age_years_months(400) == "1y 1m"
    assert format_age_years_months(20) == "0m"


def test_compute_dim_at_cull_lactating():
    event_date = dt.date(2026, 3, 15)
    fdat = dt.date(2025, 6, 1)
    assert compute_dim_at_cull(
        lact=2,
        event_date=event_date,
        bdat=dt.date(2020, 1, 1),
        fdat=fdat,
        dim_field=287.0,
    ) == 287
    assert compute_dim_at_cull(
        lact=2,
        event_date=event_date,
        bdat=dt.date(2020, 1, 1),
        fdat=fdat,
        dim_field=None,
    ) == (event_date - fdat).days


def test_compute_dim_at_cull_youngstock():
    event_date = dt.date(2026, 3, 15)
    bdat = dt.date(2025, 9, 1)
    assert compute_dim_at_cull(
        lact=0,
        event_date=event_date,
        bdat=bdat,
        fdat=None,
        dim_field=None,
    ) == (event_date - bdat).days


def test_compute_price_per_kg():
    assert compute_price_per_kg(1234.56, 350.5) == 3.52
    assert compute_price_per_kg(100, 0) is None


def test_is_plausible_carcass_row_rejects_price_per_kg():
    assert is_plausible_carcass_row(387.7, 2054.75) is True
    assert is_plausible_carcass_row(5.30, 2054.75) is False
    assert is_plausible_carcass_row(4.0, 911.01) is False


def test_is_rejected_sale_when_weight_matches_reject_and_amount_zero():
    assert is_rejected_sale(287.5, 287.5, 0.0) is True
    assert is_rejected_sale(402.0, 0.0, 2130.58) is False
    assert is_rejected_sale(287.5, 286.0, 0.0) is False
    assert is_acceptable_sale_line(287.5, 287.5, 0.0) is True
    assert is_acceptable_sale_line(402.0, 0.0, 2130.58) is True


def test_parse_rejected_sale_row():
    """Rejected animals have zero amount and reject kgs equal to cold weight."""
    table_header = [
        [
            "Carcass",
            "Tag Number",
            "Dress",
            "Breed",
            "Cat",
            "Grade",
            "Grader",
            "Kill Date",
            "Age",
            "QAS",
            "Cold Weight KG",
            "Reject Kgs",
            "Price",
            "Amount",
        ]
    ]
    table_body = [
        [
            "501",
            None,
            "UK752261210100",
            "UK",
            None,
            "HF",
            "D",
            "-O4L",
            None,
            "",
            "04/06/2026",
            "36",
            "YES",
            "287.5",
            "287.5",
            "4.10",
            "0.00",
        ]
    ]
    warnings: list[str] = []
    lines, header = _parse_table_rows(table_header, warnings)
    assert header is not None
    more_lines, _ = _parse_table_rows(table_body, warnings, shared_header=header)
    assert len(more_lines) == 1
    row = more_lines[0]
    assert row["etag"] == "UK752261210100"
    assert row["cold_weight_kg"] == 287.5
    assert row["reject_kg"] == 287.5
    assert row["amount_gbp"] == 0.0
    assert row["is_rejected"] is True


def test_parse_cattle_sale_pdf_text_fallback(monkeypatch):
    def fake_extract(_content: bytes) -> str:
        return SAMPLE_TEXT

    monkeypatch.setattr("app.services.cattle_sale_pdf._extract_text", fake_extract)
    monkeypatch.setattr("app.services.cattle_sale_pdf._extract_tables", lambda _c: [])

    result = parse_cattle_sale_pdf(b"fake", mailbox_farm="GAD")
    assert result["farm"] == "GAD"
    assert result["sale_date"] == dt.date(2026, 3, 15)
    assert len(result["lines"]) == 2
    assert result["lines"][0]["etag"] == "UK752261609397"
    assert result["lines"][0]["cold_weight_kg"] == 350.5
    assert result["lines"][0]["amount_gbp"] == 1234.56


def test_parse_misaligned_continuation_table_row():
    """Eurofarm PDFs sometimes split rows across tables with shifted columns."""
    table_header = [
        [
            "Carcass",
            "Tag Number",
            "Dress",
            "Breed",
            "Cat",
            "Grade",
            "Grader",
            "Kill Date",
            "Age",
            "QAS",
            "Cold Weight KG",
            "Reject Kgs",
            "Price",
            "Amount",
        ]
    ]
    table_body = [
        [
            "498",
            None,
            "UK740651125211",
            "UK",
            None,
            "HF",
            "D",
            "-O4H",
            None,
            "",
            "04/06/2026",
            "48",
            "YES",
            "402.0",
            "0.000",
            None,
            "5.30",
            "2130.58",
        ]
    ]
    warnings: list[str] = []
    lines, header = _parse_table_rows(table_header, warnings)
    assert header is not None
    more_lines, _ = _parse_table_rows(table_body, warnings, shared_header=header)
    assert len(more_lines) == 1
    assert more_lines[0]["etag"] == "UK740651125211"
    assert more_lines[0]["cold_weight_kg"] == 402.0
    assert more_lines[0]["amount_gbp"] == 2130.58


def test_parse_real_gad_sample_pdf():
    from pathlib import Path

    path = Path("Cheque Payment Report GREEN ACRE 17.06.26.pdf")
    if not path.is_file():
        return
    result = parse_cattle_sale_pdf(path.read_bytes(), mailbox_farm="GAD")
    assert result["sale_date"] == dt.date(2026, 6, 22)
    assert len(result["lines"]) == 3
    assert abs(sum(line["amount_gbp"] for line in result["lines"]) - 4878.80) < 0.01
    assert result["lines"][0]["cold_weight_kg"] == 387.7


def test_list_cattle_sales_matches_sold_event_when_herd_etag_lacks_uk_prefix() -> None:
    """CM herd exports often store ETAG without the UK prefix."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(
        CowEvent(
            farm="CM",
            cow_id="724069",
            etag="740651724069",
            event="SOLD",
            event_date=dt.date(2026, 6, 18),
            dest="EUROFARM",
            remark="CAR16",
            gndr="M",
            bdat=dt.date(2022, 1, 1),
            lact=0,
            cbrd=1,
        )
    )
    session.add(
        CattleSaleLine(
            farm="CM",
            etag="UK740651724069",
            sale_date=dt.date(2026, 6, 22),
            cold_weight_kg=350.0,
            amount_gbp=1500.0,
        )
    )
    session.commit()

    result = list_cattle_sales(session, farms=["CM"])
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["event_matched"] is True
    assert row["cow_id"] == "724069"
    assert row["event_date"] == "2026-06-18"

    session.close()
