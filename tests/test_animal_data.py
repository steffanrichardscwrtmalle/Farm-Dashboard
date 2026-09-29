"""Genetics Animal Data table: farm groups, exclusions, and exports."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GenomicResult, HerdInventory
from app.services.animal_data import (
    COLUMNS,
    build_animal_data_csv,
    build_animal_data_xlsx,
    farm_group_label,
    list_animal_data,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _animal(**overrides) -> HerdInventory:
    values = dict(
        farm="CM",
        cow_id="100",
        etag="UK740651300100",
        bdat=dt.date(2024, 3, 2),
        lact=0,
        dreg="UK111111111111",
        sreg="UK222222222222",
        mgreg="UK333333333333",
        ggreg="UK444444444444",
        fdat=dt.date(2026, 1, 10),
        due=dt.date(2026, 10, 1),
        hdat=dt.date(2025, 12, 20),
        rpro="BRED",
        cbrd=90,
        dim=40,
        tbrd=1,
        dcc=50,
        rc=5,
    )
    values.update(overrides)
    return HerdInventory(**values)


def test_farm_group_uses_lactation_and_dcc() -> None:
    assert farm_group_label("CM", 0, 0) == "CM YS"
    assert farm_group_label("GAD", 0, 99) == "GAD YS"
    assert farm_group_label("CM", 0, 100) == "CM Cows"
    assert farm_group_label("GAD", 3, 40) == "GAD Cows"
    assert farm_group_label("cm", 0, None) == "CM Cows"


def test_list_assigns_groups_and_keeps_requested_columns() -> None:
    session = _session()
    session.add(
        GenomicResult(hbn="740651300100", eartag="UK740651300100", cci=120.5)
    )
    session.add(_animal(cow_id="10", lact=0, dcc=20))
    session.add(_animal(farm="GAD", cow_id="20", etag="UK740651300200", lact=0, dcc=0))
    session.add(_animal(cow_id="30", etag="UK740651300300", lact=2, dcc=80))
    session.add(_animal(farm="GAD", cow_id="40", etag="UK740651300400", lact=1, dcc=200))
    session.commit()

    result = list_animal_data(session)
    assert result["total"] == 4
    by_id = {row["id"]: row for row in result["rows"]}
    assert by_id["10"]["farm_group"] == "CM YS"
    assert by_id["20"]["farm_group"] == "GAD YS"
    assert by_id["30"]["farm_group"] == "CM Cows"
    assert by_id["40"]["farm_group"] == "GAD Cows"
    assert by_id["10"]["cci"] == 120.5
    assert by_id["20"]["cci"] is None
    assert by_id["10"]["mgreg"] == "UK333333333333"
    assert by_id["10"]["ggreg"] == "UK444444444444"
    assert by_id["10"]["bdat"] == "2024-03-02"
    assert [row["id"] for row in result["rows"]] == ["30", "10", "40", "20"]
    session.close()


def test_list_excludes_bulls_males_and_beef() -> None:
    session = _session()
    session.add(_animal(cow_id="keep", etag="UK1"))
    session.add(_animal(cow_id="bull", etag="UK2", rpro="BULL"))
    session.add(_animal(cow_id="bull-case", etag="UK3", rpro=" bull "))
    session.add(_animal(cow_id="male", etag="UK4", rc=8, rpro=""))
    session.add(_animal(cow_id="beef", etag="UK5", cbrd=102))
    session.add(_animal(cow_id="border", etag="UK6", cbrd=101))
    session.commit()

    result = list_animal_data(session, farms=["CM"])
    assert {row["id"] for row in result["rows"]} == {"keep", "border"}
    session.close()


def test_list_filters_farm_and_group() -> None:
    session = _session()
    session.add(_animal(cow_id="ys", lact=0, dcc=10))
    session.add(_animal(cow_id="cow", etag="UK9", lact=2, dcc=10))
    session.add(_animal(farm="GAD", cow_id="gad", etag="UK8", lact=4, dcc=10))
    session.commit()

    cows = list_animal_data(session, groups=["CM Cows"])
    assert [row["id"] for row in cows["rows"]] == ["cow"]

    gad = list_animal_data(session, farms=["GAD"])
    assert [row["id"] for row in gad["rows"]] == ["gad"]
    assert gad["rows"][0]["farm_group"] == "GAD Cows"
    session.close()


def test_cci_matches_genomic_ear_tag_digits() -> None:
    session = _session()
    session.add(_animal(cow_id="spaced", etag=" UK740651300100 "))
    session.add(
        GenomicResult(hbn="999", eartag="UK 740651 300100", cci=88)
    )
    session.add(_animal(cow_id="other", etag="UK740651399999"))
    session.commit()

    result = list_animal_data(session, farms=["CM"])
    by_id = {row["id"]: row for row in result["rows"]}
    assert by_id["spaced"]["cci"] == 88
    assert by_id["other"]["cci"] is None
    session.close()


def test_exports_include_headers_and_values() -> None:
    session = _session()
    session.add(GenomicResult(hbn="740651300100", eartag="UK740651300100", cci=120.5))
    session.add(_animal())
    session.commit()
    rows = list_animal_data(session)["rows"]

    csv_text = build_animal_data_csv(rows).decode("utf-8-sig")
    header = ",".join(label for _key, label in COLUMNS)
    assert csv_text.splitlines()[0] == header
    assert "UK333333333333" in csv_text
    assert "CM YS" in csv_text

    workbook = load_workbook(io.BytesIO(build_animal_data_xlsx(rows)))
    sheet = workbook.active
    assert [cell.value for cell in sheet[1]] == [label for _key, label in COLUMNS]
    assert sheet["E2"].value == 120.5
    assert sheet["C2"].value.date() == dt.date(2024, 3, 2)
    assert sheet["R2"].value == "CM YS"
    session.close()


def test_animal_data_page_is_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    nav = (root / "templates" / "base.html").read_text(encoding="utf-8")
    page = (root / "templates" / "genetics" / "animal_data.html").read_text(encoding="utf-8")
    routes = (root / "app" / "api" / "genetics_routes.py").read_text(encoding="utf-8")

    assert '@app.get("/genetics/animal-data"' in main
    assert 'href="/genetics/animal-data"' in nav
    assert "Animal Data" in nav
    pedigree_idx = nav.find("Pedigree Registrations")
    animal_idx = nav.find("Animal Data")
    bull_idx = nav.find("Bull Search")
    assert pedigree_idx < animal_idx < bull_idx
    assert 'id="animal-data-table"' in page
    assert 'id="download-csv-btn"' in page
    assert 'id="download-xlsx-btn"' in page
    for label in ("ID", "ETAG", "BDAT", "LACT", "CCI", "MGREG", "GGREG", "Farm Group"):
        assert f">{label}<" in page
    assert '@router.get("/animal-data")' in routes
    assert '@router.get("/animal-data/export.csv")' in routes
    assert '@router.get("/animal-data/export.xlsx")' in routes
