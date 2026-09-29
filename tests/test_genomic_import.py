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

    uploaded: list[str] = []
    monkeypatch.setattr(
        "app.services.genomic_import.upload_herd_file",
        lambda path, _content, **_kwargs: uploaded.append(path),
    )

    result = import_genomic_results(session, force=True)
    assert result["skipped"] is False
    assert result["rows_imported"] == 1
    assert result["animal_data_csv"] is None
    assert uploaded == []
    assert session.scalar(select(GenomicResult.hbn)) == "999"
    stored = session.scalar(
        select(AppSetting.value).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY)
    )
    assert stored == _fingerprint("Genomic Results/file.xlsx", "2026-07-01T10:00:00Z")

    session.close()


def test_changed_genomic_file_exports_animal_data_csv(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    from app.models import HerdInventory

    session.add(
        HerdInventory(
            farm="CM",
            cow_id="10",
            etag="UK999",
            lact=1,
            aged=300,
            gender="Female",
        )
    )
    session.add(
        AppSetting(
            key=GENOMIC_SOURCE_SETTING_KEY,
            value=_fingerprint("Genomic Results/old.xlsx", "2026-06-01T10:00:00Z"),
        )
    )
    session.commit()

    monkeypatch.setattr("app.services.genomic_import.graph_is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.genomic_import.find_newest_herd_file_meta",
        lambda *_a, **_k: {
            "relative_path": "Genomic Results/new.xlsx",
            "name": "new.xlsx",
            "last_modified": "2026-07-01T10:00:00Z",
        },
    )
    monkeypatch.setattr(
        "app.services.genomic_import.download_herd_file",
        lambda _path: b"fake",
    )

    import pandas as pd

    monkeypatch.setattr(
        "app.services.genomic_import.pd.read_excel",
        lambda _buf, sheet_name=None: pd.DataFrame(
            {
                "HBN": [999],
                "EarTag Number": ["UK999"],
                "Sire": ["SireA"],
                "Sire Reg No ID": ["REG1"],
                "PLI": [250.0],
            }
        ),
    )

    uploaded: list[tuple[str, bytes]] = []

    def _capture(path: str, content: bytes, **_kwargs) -> None:
        uploaded.append((path, content))

    monkeypatch.setattr("app.services.genomic_import.upload_herd_file", _capture)

    result = import_genomic_results(session)
    assert result["skipped"] is False
    assert result["animal_data_csv"] == "Genomic Results/animal_data.csv"
    assert uploaded[0][0] == "Genomic Results/animal_data.csv"
    text = uploaded[0][1].decode("utf-8-sig")
    assert text.splitlines()[0].startswith("ID,ETAG,SREG")
    assert "UK999" in text
    assert "REG1" in text
    session.close()


def test_blank_trait_cells_are_stored_as_null() -> None:
    import datetime as dt

    import pandas as pd

    from app.services.genomic_import import _dataframe_to_mappings

    frame = pd.DataFrame(
        {
            "HBN": [999],
            "EarTag Number": ["UK999"],
            "Milk": [float("nan")],
            "PLI": [250.0],
        }
    )
    rows = _dataframe_to_mappings(frame, dt.datetime(2026, 9, 29))
    assert rows[0]["milk_kg"] is None
    assert rows[0]["pli"] == 250.0
