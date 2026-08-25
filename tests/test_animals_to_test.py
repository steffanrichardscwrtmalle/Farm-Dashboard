"""Animals-to-test worklist (untested dairy heifers)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GenomicResult, HerdInventory
from app.services.animals_to_test import etag4, list_animals_to_test


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _inventory(**overrides) -> HerdInventory:
    values = dict(
        farm="CM",
        cow_id="100",
        etag="UK740651324400",
        gender="Female",
        cbrd=90,
        lact=0,
        aged=120,
        pen="12",
        gid=None,
        gtest=None,
        subd=None,
    )
    values.update(overrides)
    return HerdInventory(**values)


def test_etag4_uses_last_four_digits() -> None:
    assert etag4("UK740651324400") == "4400"
    assert etag4(" 435259 ") == "5259"
    assert etag4("") == ""


def test_list_includes_blank_gid_gtest_subd_without_genomic() -> None:
    session = _session()
    session.add(_inventory())
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["id"] == "100"
    assert row["etag4"] == "4400"
    assert row["aged"] == 120
    assert row["pen"] == "12"
    assert row["farm"] == "CM"
    session.close()


def test_list_treats_zero_gid_as_untested() -> None:
    session = _session()
    session.add(_inventory(cow_id="101", etag="UK740651324401", gid="0"))
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 1
    session.close()


def test_list_excludes_animals_with_gid_or_submission() -> None:
    session = _session()
    session.add(_inventory(cow_id="1", etag="UK740651000001", gid="TSU123"))
    session.add(
        _inventory(
            cow_id="2",
            etag="UK740651000002",
            gtest=dt.date(2026, 1, 15),
        )
    )
    session.add(
        _inventory(
            cow_id="3",
            etag="UK740651000003",
            subd=dt.date(2026, 1, 20),
        )
    )
    session.add(_inventory(cow_id="4", etag="UK740651000004"))
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 1
    assert result["rows"][0]["id"] == "4"
    session.close()


def test_list_excludes_animals_that_already_have_genomic_results() -> None:
    session = _session()
    session.add(_inventory(etag="UK740651324400"))
    session.add(
        GenomicResult(hbn="740651324400", eartag="UK740651324400")
    )
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 0
    session.close()


def test_list_excludes_beef_males_and_lactating() -> None:
    session = _session()
    session.add(_inventory(cow_id="beef", etag="UK740651111111", cbrd=110))
    session.add(
        _inventory(cow_id="bull", etag="UK740651222222", gender="Male")
    )
    session.add(_inventory(cow_id="cow", etag="UK740651444444", lact=1))
    session.add(_inventory(cow_id="heifer", etag="UK740651333333"))
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 1
    assert result["rows"][0]["id"] == "heifer"
    session.close()


def test_list_applies_default_age_window() -> None:
    session = _session()
    session.add(_inventory(cow_id="young", etag="UK740651000010", aged=45))
    session.add(_inventory(cow_id="ok", etag="UK740651000020", aged=80))
    session.add(_inventory(cow_id="old", etag="UK740651000030", aged=1000))
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert result["total"] == 1
    assert result["rows"][0]["id"] == "ok"

    widened = list_animals_to_test(
        session, farms=["CM"], min_aged=0, max_aged=2000
    )
    assert {row["id"] for row in widened["rows"]} == {"young", "ok", "old"}
    session.close()


def test_list_limits_gad_to_born_on_or_after_2025_09_16() -> None:
    session = _session()
    session.add(
        _inventory(
            farm="GAD",
            cow_id="old-gad",
            etag="UK740651000101",
            bdat=dt.date(2025, 9, 15),
        )
    )
    session.add(
        _inventory(
            farm="GAD",
            cow_id="no-bdat",
            etag="UK740651000102",
            bdat=None,
        )
    )
    session.add(
        _inventory(
            farm="GAD",
            cow_id="new-gad",
            etag="UK740651000103",
            bdat=dt.date(2025, 9, 16),
        )
    )
    session.add(
        _inventory(
            farm="CM",
            cow_id="old-cm",
            etag="UK740651000104",
            bdat=dt.date(2025, 6, 1),
        )
    )
    session.commit()

    result = list_animals_to_test(session, farms=["CM", "GAD"])
    assert {row["id"] for row in result["rows"]} == {"new-gad", "old-cm"}
    session.close()


def test_list_defaults_to_etag4_ascending() -> None:
    session = _session()
    session.add(_inventory(cow_id="b", etag="UK740651000900"))
    session.add(_inventory(cow_id="a", etag="UK740651000012"))
    session.commit()

    result = list_animals_to_test(session, farms=["CM"])
    assert [row["etag4"] for row in result["rows"]] == ["0012", "0900"]
    session.close()


def test_animals_to_test_page_is_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    nav = (root / "templates" / "base.html").read_text(encoding="utf-8")
    page = (root / "templates" / "genetics" / "animals_to_test.html").read_text(
        encoding="utf-8"
    )
    routes = (root / "app" / "api" / "genetics_routes.py").read_text(encoding="utf-8")
    assert '@app.get("/genetics/animals-to-test"' in main
    assert 'href="/genetics/animals-to-test"' in nav
    assert "Animals To Test" in nav
    genomic_idx = nav.find("Genomic Progress")
    to_test_idx = nav.find("Animals To Test")
    pending_idx = nav.find("Pending Results")
    assert genomic_idx < to_test_idx < pending_idx
    assert 'id="to-test-table"' in page
    assert ">AGED<" in page
    assert ">ETAG4<" in page
    assert ">PEN<" in page
    assert "Genomic ID" in page
    assert 'id="print-btn"' in page
    assert 'id="min-aged-input"' in page
    assert 'id="max-aged-input"' in page
    assert 'value="60"' in page
    assert 'value="999"' in page
    assert 'id="farm-slicer"' in page
    assert 'sortKey = "etag4"' in page
    assert 'sortDir = "asc"' in page
    assert "height: 6.2mm" in page
    assert "@page { size: A4 portrait" in page
    assert '@router.get("/animals-to-test")' in routes
    assert '@router.get("/animals-to-test/export.csv")' in routes
