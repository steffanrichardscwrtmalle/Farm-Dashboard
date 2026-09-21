"""Genomic progress graphs, including computed £CM."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GenomicResult, HerdInventory
from app.services.custom_indexes import cm_index, save_index_settings
from app.services.genomic_progress import (
    build_genomic_progress,
    build_genomic_scatter,
    list_traits,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed(db) -> GenomicResult:
    genomic = GenomicResult(
        hbn="740651324400",
        eartag="UK740651324400",
        milk_kg=775,
        fat_pct=0.28,
        protein_pct=0.12,
        fertility_index=5.5,
        life_span=101,
        scc=-11,
        mastitis=-2,
        pli=420,
    )
    db.add(genomic)
    db.add(
        HerdInventory(
            farm="CM",
            cow_id="100",
            etag="UK740651324400",
            bdat=dt.date(2024, 1, 15),
        )
    )
    db.commit()
    return genomic


def test_list_traits_includes_cm_index() -> None:
    traits = list_traits()
    assert {"key": "cm_index", "label": "£CM"} in traits
    assert {"key": "pli", "label": "PLI"} in traits


def test_genomic_progress_computes_cm_from_index_formula() -> None:
    db = _session()
    genomic = _seed(db)
    result = build_genomic_progress(db, trait="cm_index", farms=["CM"])
    assert result["trait"] == "cm_index"
    assert result["trait_label"] == "£CM"
    assert result["count"] == 1
    point = result["points"]["CM"][0]
    assert round(point["y"], 4) == round(cm_index(genomic), 4)
    db.close()


def test_genomic_progress_uses_saved_cm_settings() -> None:
    db = _session()
    genomic = _seed(db)
    default = build_genomic_progress(db, trait="cm_index", farms=["CM"])
    save_index_settings(db, {"cm": {"volume_price": 0}})
    updated = build_genomic_progress(db, trait="cm_index", farms=["CM"])
    assert updated["points"]["CM"][0]["y"] != default["points"]["CM"][0]["y"]
    assert round(updated["points"]["CM"][0]["y"], 4) == round(
        cm_index(genomic, {"cm": {"volume_price": 0}}), 4
    )
    db.close()


def test_genomic_scatter_accepts_cm_on_either_axis() -> None:
    db = _session()
    genomic = _seed(db)
    result = build_genomic_scatter(
        db, x_trait="pli", y_trait="cm_index", farms=["CM"]
    )
    assert result["y_label"] == "£CM"
    assert result["count"] == 1
    point = result["points"]["CM"][0]
    assert point["x"] == 420
    assert round(point["y"], 4) == round(cm_index(genomic), 4)
    db.close()
