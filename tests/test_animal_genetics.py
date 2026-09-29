"""Animal Data joins inventory identity to genomic traits."""

from __future__ import annotations

import io
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GenomicResult, HerdInventory
from app.services.animal_genetics import (
    COLUMNS,
    build_animal_genetics_csv,
    build_animal_genetics_xlsx,
    list_animal_genetics,
)
from app.services.custom_indexes import cm_index


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _animal(**overrides) -> HerdInventory:
    values = dict(
        farm="CM",
        cow_id="10",
        etag="UK740651300100",
        lact=2,
        aged=840,
    )
    values.update(overrides)
    return HerdInventory(**values)


def _genomic(**overrides) -> GenomicResult:
    values = dict(
        hbn="740651300100",
        eartag="UK740651300100",
        milk_kg=775,
        fat_kg=42.5,
        protein_kg=30.2,
        fat_pct=0.28,
        protein_pct=0.12,
        pli=400,
        fertility_index=5.5,
        scc=-11,
        life_span=101,
        mastitis=-2,
        stature=1.2,
        chest_width=0.4,
        sire_reg="UK333333333333",
    )
    values.update(overrides)
    return GenomicResult(**values)


def test_unknown_sreg_is_shown_as_bullho() -> None:
    session = _session()
    session.add(_genomic(sire_reg="UUUUUUUUUUUU"))
    session.add(_animal())
    session.add(
        _genomic(hbn="740651300200", eartag="UK740651300200", sire_reg=" uuuu 123 ")
    )
    session.add(_animal(cow_id="20", etag="UK740651300200"))
    session.add(_animal(cow_id="30", etag="UK740651300300"))
    session.add(
        _genomic(hbn="740651300300", eartag="UK740651300300", sire_reg="UK333333333333")
    )
    session.commit()

    result = list_animal_genetics(session, farms=["CM"])
    by_id = {row["id"]: row for row in result["rows"]}
    assert by_id["10"]["sreg"] == "BULLHO"
    assert by_id["20"]["sreg"] == "BULLHO"
    assert by_id["30"]["sreg"] == "UK333333333333"
    session.close()


def test_list_joins_inventory_and_genomic_by_ear_tag() -> None:
    session = _session()
    genomic = _genomic(hbn="999", eartag="UK 740651 300100")
    session.add(_animal(etag=" UK740651300100 "))
    session.add(genomic)
    session.add(_animal(cow_id="20", etag="UK740651300200", lact=0, aged=120))
    session.commit()

    result = list_animal_genetics(session, farms=["CM"])
    by_id = {row["id"]: row for row in result["rows"]}

    matched = by_id["10"]
    assert matched["etag"] == "UK740651300100"
    assert matched["sreg"] == "UK333333333333"
    assert matched["lact"] == 2
    assert matched["age"] == 28
    assert matched["cm"] == int(round(cm_index(genomic)))
    assert matched["pli"] == 400
    assert matched["milk_kg"] == 775
    assert matched["fat_kg"] == 42.5
    assert matched["protein_kg"] == 30.2
    assert matched["fat_pct"] == 0.28
    assert matched["protein_pct"] == 0.12
    assert matched["fertility_index"] == 5.5
    assert matched["scc"] == -11
    assert matched["life_span"] == 101
    assert matched["mastitis"] == -2
    assert matched["stature"] == 1.2
    assert matched["chest_width"] == 0.4

    assert "20" not in by_id
    assert [row["id"] for row in result["rows"]] == ["10"]
    session.close()


def test_blank_genomic_traits_still_produce_a_whole_cm() -> None:
    session = _session()
    session.add(_animal())
    session.add(_genomic(milk_kg=float("nan"), fat_pct=float("nan")))
    session.commit()

    result = list_animal_genetics(session, farms=["CM"])
    assert isinstance(result["rows"][0]["cm"], int)
    session.close()


def test_exports_use_animal_data_headers() -> None:
    session = _session()
    session.add(_genomic())
    session.add(_animal())
    session.commit()
    rows = list_animal_genetics(session)["rows"]

    csv_text = build_animal_genetics_csv(rows).decode("utf-8-sig")
    assert csv_text.splitlines()[0] == ",".join(label for _key, label in COLUMNS)

    workbook = load_workbook(io.BytesIO(build_animal_genetics_xlsx(rows)))
    sheet = workbook.active
    assert sheet.title == "Animal Data"
    assert [cell.value for cell in sheet[1]] == [label for _key, label in COLUMNS]
    assert sheet["C2"].value == "UK333333333333"
    assert sheet["E2"].value == 28
    assert sheet["F2"].value == int(round(cm_index(_genomic())))
    session.close()


def test_animal_genetics_page_is_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    nav = (root / "templates" / "base.html").read_text(encoding="utf-8")
    page = (root / "templates" / "genetics" / "animal_genetics.html").read_text(
        encoding="utf-8"
    )
    routes = (root / "app" / "api" / "genetics_routes.py").read_text(encoding="utf-8")

    assert '@app.get("/genetics/animal-data"' in main
    assert 'href="/genetics/animal-data"' in nav
    guide_idx = nav.find("Reports for Mating Guide")
    animal_idx = nav.find(">Animal Data<")
    bull_idx = nav.find("Bull Search")
    assert guide_idx < animal_idx < bull_idx
    assert 'id="animal-genetics-table"' in page
    assert 'id="page-next"' in page
    assert "PAGE_SIZE" in page
    assert 'let sortKey = "cm"' in page
    assert 'let sortDir = "desc"' in page
    for label in (
        "ID",
        "ETAG",
        "SREG",
        "LACT",
        "Age",
        "£CM",
        "PLI",
        "Milk Kg",
        "Fat Kg",
        "Protein kg",
        "Fat %",
        "Protein %",
        "Fertility Index",
        "SCC",
        "Lifespan",
        "Mastitis",
        "Stature",
        "Chest Width",
    ):
        assert f">{label}<" in page
    assert '@router.get("/animal-data")' in routes
