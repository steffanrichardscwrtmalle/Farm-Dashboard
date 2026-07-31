"""Tests for genomic import skip-when-unchanged behaviour."""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import AppSetting, Base, GenomicResult
from app.services.genomic_import import (
    GENOMIC_SOURCE_SETTING_KEY,
    _fingerprint,
    import_genomic_results,
)


def test_import_genomic_results_skips_when_fingerprint_matches(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    session.add(
        GenomicResult(hbn="123", eartag="UK1", pli=100.0)
    )
    session.add(
        AppSetting(
            key=GENOMIC_SOURCE_SETTING_KEY,
            value=_fingerprint("Genomic Results/file.xlsx", "2026-07-01T10:00:00Z"),
        )
    )
    session.commit()

    monkeypatch.setattr(
        "app.services.genomic_import.graph_is_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.genomic_import.find_newest_herd_file_meta",
        lambda *_a, **_k: {
            "relative_path": "Genomic Results/file.xlsx",
            "name": "file.xlsx",
            "last_modified": "2026-07-01T10:00:00Z",
        },
    )

    def _should_not_download(_path: str) -> bytes:
        raise AssertionError("download should be skipped when source is unchanged")

    monkeypatch.setattr(
        "app.services.genomic_import.download_herd_file",
        _should_not_download,
    )

    result = import_genomic_results(session)
    assert result["skipped"] is True
    assert result["reason"] == "source_unchanged"
    assert result["rows_imported"] == 1
    assert session.scalar(select(GenomicResult.hbn)) == "123"

    session.close()


def test_import_genomic_results_force_reimports(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    session.add(
        AppSetting(
            key=GENOMIC_SOURCE_SETTING_KEY,
            value=_fingerprint("Genomic Results/file.xlsx", "2026-07-01T10:00:00Z"),
        )
    )
    session.commit()

    monkeypatch.setattr(
        "app.services.genomic_import.graph_is_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.genomic_import.find_newest_herd_file_meta",
        lambda *_a, **_k: {
            "relative_path": "Genomic Results/file.xlsx",
            "name": "file.xlsx",
            "last_modified": "2026-07-01T10:00:00Z",
        },
    )

    import pandas as pd

    monkeypatch.setattr(
        "app.services.genomic_import.download_herd_file",
        lambda _path: b"fake",
    )

    def fake_read_excel(_buf, sheet_name=None):
        return pd.DataFrame(
            {
                "HBN": [999],
                "EarTag Number": ["UK999"],
                "Sire": ["SireA"],
                "Sire Reg No ID": ["REG1"],
                "PLI": [250.0],
            }
        )

    monkeypatch.setattr("app.services.genomic_import.pd.read_excel", fake_read_excel)

    result = import_genomic_results(session, force=True)
    assert result["skipped"] is False
    assert result["rows_imported"] == 1
    assert session.scalar(select(GenomicResult.hbn)) == "999"
    stored = session.scalar(
        select(AppSetting.value).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY)
    )
    assert stored == _fingerprint("Genomic Results/file.xlsx", "2026-07-01T10:00:00Z")

    session.close()
