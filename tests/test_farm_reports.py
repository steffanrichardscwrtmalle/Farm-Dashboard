"""Farm Reports: Heifers To Scan from inventory."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, HerdInventory
from app.services.farm_reports import (
    BLANK_PEN,
    PDF_ROWS_PER_PAGE,
    apply_pen_filter,
    build_heifers_to_scan_pdf,
    build_heifers_to_scan_xlsx,
    etag5,
    farm_reports,
    heifers_to_scan,
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _heifer(
    *,
    farm: str = "CM",
    cow_id: str,
    etag: str | None = "UK740651324400",
    category: str = "Youngstock",
    rc: float | None = 3,
    dslh: float | None = 40,
    rpro: str | None = "BRED",
    remark: str | None = "BRED",
    pen: str | None = "12",
    aged: int | None = 420,
    tbrd: int | None = 1,
) -> HerdInventory:
    return HerdInventory(
        farm=farm,
        cow_id=cow_id,
        etag=etag,
        category=category,
        rc=rc,
        dslh=dslh,
        rpro=rpro,
        remark=remark,
        pen=pen,
        aged=aged,
        tbrd=tbrd,
    )


def test_etag5_strips_spaces_and_uses_last_five_digits() -> None:
    assert etag5("UK740651324400") == "24400"
    assert etag5("UK740651324400     ") == "24400"
    assert etag5("UK 7406 5132 4400") == "24400"
    assert etag5(None) is None


def test_heifers_to_scan_filters_youngstock_rc_and_dslh() -> None:
    db = _db()
    db.add_all(
        [
            _heifer(cow_id="100", rc=3, dslh=32, etag="UK740651324400     "),
            _heifer(cow_id="101", rc=4, dslh=50, etag="UK740651300111"),
            _heifer(cow_id="102", rc=3, dslh=31),
            _heifer(cow_id="103", rc=3, dslh=40, category="Dairy"),
            _heifer(cow_id="104", rc=2, dslh=80),
            _heifer(cow_id="105", farm="GAD", rc=3, dslh=90),
        ]
    )
    db.commit()

    result = heifers_to_scan(db, "CM")
    ids = [row["id"] for row in result["rows"]]
    assert result["count"] == 2
    assert ids == ["100", "101"]
    assert result["rows"][0]["etag5"] == "24400"
    assert result["rows"][1]["etag5"] == "00111"
    assert result["rows"][0]["dslh"] == 32
    assert result["rows"][0]["rpro"] == "BRED"
    assert result["rows"][0]["remark"] == "BRED"
    assert result["rows"][0]["pen"] == "12"
    assert result["rows"][0]["tbrd"] == 1


def test_heifers_to_scan_pen_filter_and_blank_pen() -> None:
    db = _db()
    db.add_all(
        [
            _heifer(cow_id="1", pen="12"),
            _heifer(cow_id="2", pen="8"),
            _heifer(cow_id="3", pen=None),
        ]
    )
    db.commit()
    result = heifers_to_scan(db, "CM")
    assert [pen["id"] for pen in result["pens"]] == ["8", "12", BLANK_PEN]
    filtered = heifers_to_scan(db, "CM", pens=["8", BLANK_PEN])
    assert filtered["count"] == 3
    assert filtered["filtered_count"] == 2
    assert {row["id"] for row in filtered["rows"]} == {"2", "3"}


def test_apply_pen_filter_empty_selection() -> None:
    rows = [{"id": "1", "pen": "12"}]
    assert apply_pen_filter(rows, ["__no_match__"]) == []


def test_farm_reports_widget_count_matches_table() -> None:
    db = _db()
    db.add(_heifer(cow_id="200", rc=4, dslh=33))
    db.commit()
    payload = farm_reports(db, "cm")
    assert payload["farm"] == "CM"
    assert payload["farm_label"] == "Cwrt Malle"
    assert payload["widgets"][0]["id"] == "heifers-to-scan"
    assert payload["widgets"][0]["count"] == 1
    assert payload["heifers_to_scan"]["count"] == 1


def test_heifers_to_scan_xlsx_export() -> None:
    db = _db()
    db.add(_heifer(cow_id="1", remark="SCAN"))
    db.commit()
    content = build_heifers_to_scan_xlsx(heifers_to_scan(db, "CM"))
    assert content[:2] == b"PK"


def test_heifers_to_scan_pdf_export() -> None:
    import pytest

    pytest.importorskip("reportlab")
    db = _db()
    db.add_all(
        [
            _heifer(cow_id=str(i), pen="12" if i < 25 else "8")
            for i in range(PDF_ROWS_PER_PAGE + 5)
        ]
    )
    db.commit()
    report = heifers_to_scan(db, "CM")
    pdf = build_heifers_to_scan_pdf(report)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 200
    assert report["count"] == PDF_ROWS_PER_PAGE + 5
