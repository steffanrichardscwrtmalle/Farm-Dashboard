from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import AhdbBull, Base
from app.services.custom_indexes import save_index_settings
from app.services.ahdb_bulls import (
    AhdbBullsError,
    AhdbReport,
    bull_from_row,
    clean_bull_name,
    ensure_imported,
    fetch_and_write,
    fetch_report,
    import_reports,
    list_bulls,
    write_csv,
)


def _payload() -> dict:
    return {
        "metadata": [
            {"name": "rank", "title": "Rank"},
            {"name": "bull_name", "title": "Bull Name"},
            {"name": "pli", "title": "£PLI"},
        ],
        "data": [
            {
                "rank": 1,
                "bull_name": "  OCD TROOPER SHEEPSTER  ",
                "pli": 756,
                "bull_hbn_emn": "3236792832",
                "sire_name": "TROOPER",
            }
        ],
    }


def test_fetch_report_maps_columns_and_strips_names() -> None:
    response = MagicMock()
    response.json.return_value = _payload()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = response

    report = fetch_report("genomic", client=client)

    assert report.label.startswith("HOL bulls")
    assert report.rows[0]["pli"] == 756
    titles = [title for _, title in report.columns]
    assert titles[:3] == ["Rank", "Bull Name", "£PLI"]
    assert "HBN" in titles
    assert "Sire Name" in titles
    client.get.assert_called_once()


def test_write_csv_uses_display_headers(tmp_path: Path) -> None:
    response = MagicMock()
    response.json.return_value = _payload()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = response
    report = fetch_report("proven", client=client)

    dest = write_csv(report, tmp_path / "proven.csv")
    text = dest.read_text(encoding="utf-8-sig")
    assert "Bull Name" in text
    assert "OCD TROOPER SHEEPSTER" in text
    assert "3236792832" in text


def test_unknown_report_rejected() -> None:
    try:
        fetch_report("jersey")
    except AhdbBullsError as exc:
        assert "Unknown report" in str(exc)
    else:
        raise AssertionError("expected AhdbBullsError")


def test_fetch_and_write_both_reports(tmp_path: Path) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _payload()

    class _FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, *_args, **_kwargs):
            return _FakeResponse()

    with patch("app.services.ahdb_bulls.httpx.Client", _FakeClient):
        written = fetch_and_write(tmp_path)

    assert [path.name for _, path in written] == [
        "HOL_bulls_available_genomic.csv",
        "HOL_bulls_available_proven.csv",
        "HOL_bulls_top_international.csv",
    ]


def test_http_error_wrapped() -> None:
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("offline")
    try:
        fetch_report("genomic", client=client)
    except AhdbBullsError as exc:
        assert "Failed to fetch" in str(exc)
    else:
        raise AssertionError("expected AhdbBullsError")


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_bull_from_row_maps_traits() -> None:
    fetched = dt.datetime(2026, 8, 23, 8, 0, 0)
    bull = bull_from_row(
        "proven",
        {
            "rank": 1,
            "bull_name": "  OCD TROOPER SHEEPSTER  ",
            "bull_hbn_emn": 3236792832,
            "pli": 756,
            "pta_fat_lac_all": 48.7,
            "fertiltiy_index": 3.6,
            "straicompany": "WWS",
            "straicompanyni": "WWS",
            "sire_name": "PLAIN-KNOLL RENEGAD TROOPER",
        },
        fetched,
    )
    assert bull is not None
    assert bull.list_type == "proven"
    assert bull.hbn == "3236792832"
    assert bull.bull_name == "OCD TROOPER SHEEPSTER"
    assert bull.pli == 756
    assert bull.fat_kg == 48.7
    assert bull.fertility_index == 3.6
    assert bull.supplier_ni == "WWS"


def test_clean_bull_name_strips_casein_codes() -> None:
    assert clean_bull_name("PINE-TREE DENOVO AVON  A1A2 KCAE") == "PINE-TREE DENOVO AVON"
    assert clean_bull_name("GENOSOURCE JUMPSTART P") == "GENOSOURCE JUMPSTART P"


def test_import_and_list_replaces_list() -> None:
    db = _session()
    fetched = dt.datetime(2026, 8, 23, 8, 0, 0)
    first = AhdbReport(
        key="genomic",
        table="t",
        label="genomic",
        rows=[
            {"rank": 1, "bull_name": "OLD BULL", "bull_hbn_emn": "1", "pli": 100},
            {"rank": 2, "bull_name": "KEEP", "bull_hbn_emn": "2", "pli": 90},
        ],
        columns=[],
        fetched_at=fetched,
    )
    import_reports(db, [first])
    second = AhdbReport(
        key="genomic",
        table="t",
        label="genomic",
        rows=[{"rank": 1, "bull_name": "NEW BULL", "bull_hbn_emn": "3", "pli": 200}],
        columns=[],
        fetched_at=fetched,
    )
    import_reports(db, [second])
    listed = list_bulls(db)
    assert listed["count"] == 1
    assert listed["counts"]["genomic"] == 1
    assert listed["rows"][0]["bull_name"] == "NEW BULL"
    assert listed["rows"][0]["proof"] == "G"
    intl = AhdbReport(
        key="international",
        table="t",
        label="international",
        rows=[{"rank": 1, "bull_name": "INT SIRE", "bull_hbn_emn": "4", "pli": 300}],
        columns=[],
        fetched_at=fetched,
    )
    import_reports(db, [intl])
    listed = list_bulls(db)
    proofs = {row["bull_name"]: row["proof"] for row in listed["rows"]}
    assert proofs["NEW BULL"] == "G"
    assert proofs["INT SIRE"] == "P"
    assert "international" not in listed["counts"]
    db.close()


def test_import_dedupes_hbn_and_reranks_by_pli() -> None:
    db = _session()
    fetched = dt.datetime(2026, 8, 23, 8, 0, 0)
    import_reports(
        db,
        [
            AhdbReport(
                key="proven",
                table="t",
                label="proven",
                rows=[
                    {"rank": 9, "bull_name": "LOW", "bull_hbn_emn": "10", "pli": 100},
                    {"rank": 8, "bull_name": "HIGH", "bull_hbn_emn": "11", "pli": 400},
                ],
                columns=[],
                fetched_at=fetched,
            ),
            AhdbReport(
                key="international",
                table="t",
                label="international",
                rows=[
                    {"rank": 1, "bull_name": "HIGH", "bull_hbn_emn": "11", "pli": 400},
                    {"rank": 2, "bull_name": "MID", "bull_hbn_emn": "12", "pli": 250},
                ],
                columns=[],
                fetched_at=fetched,
            ),
        ],
    )
    listed = list_bulls(db)
    assert listed["count"] == 3
    assert listed["counts"]["proven"] == 3
    by_name = {row["bull_name"]: row for row in listed["rows"]}
    assert by_name["HIGH"]["rank"] == 1
    assert by_name["MID"]["rank"] == 2
    assert by_name["LOW"]["rank"] == 3
    assert by_name["HIGH"]["proof"] == "P"
    db.close()


def test_rank_is_overall_pli_not_per_proof() -> None:
    db = _session()
    fetched = dt.datetime(2026, 8, 23, 8, 0, 0)
    import_reports(
        db,
        [
            AhdbReport(
                key="genomic",
                table="t",
                label="genomic",
                rows=[{"rank": 1, "bull_name": "YOUNG", "bull_hbn_emn": "1", "pli": 900}],
                columns=[],
                fetched_at=fetched,
            ),
            AhdbReport(
                key="proven",
                table="t",
                label="proven",
                rows=[{"rank": 1, "bull_name": "OLD", "bull_hbn_emn": "2", "pli": 700}],
                columns=[],
                fetched_at=fetched,
            ),
        ],
    )
    listed = list_bulls(db)
    by_name = {row["bull_name"]: row for row in listed["rows"]}
    assert by_name["YOUNG"]["rank"] == 1
    assert by_name["OLD"]["rank"] == 2
    db.close()


def test_list_bulls_applies_saved_index_settings() -> None:
    db = _session()
    db.add(
        AhdbBull(
            list_type="genomic",
            hbn="1",
            bull_name="JUMP",
            milk_kg=775,
            fat_pct=0.28,
            protein_pct=0.12,
            fertility_index=5.5,
            lifespan_days=101,
            scc=-11,
            mastitis=-2,
            lameness=2.4,
            fetched_at=dt.datetime(2026, 8, 1),
        )
    )
    db.commit()
    listed = list_bulls(db)
    assert listed["index_settings"]["dp"]["fat_price"] == 2.9
    default_dp = listed["rows"][0]["dp_index"]
    save_index_settings(db, {"include_mastitis": True})
    listed = list_bulls(db)
    assert listed["index_settings"]["include_mastitis"] is True
    assert listed["rows"][0]["dp_index"] != default_dp
    db.close()


def test_bull_search_page_is_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    nav = (root / "templates" / "base.html").read_text(encoding="utf-8")
    page = (root / "templates" / "genetics" / "bull_search.html").read_text(encoding="utf-8")
    routes = (root / "app" / "api" / "genetics_routes.py").read_text(encoding="utf-8")
    assert '@app.get("/genetics/bull-search"' in main
    assert 'href="/genetics/bull-search"' in nav
    assert "Bull Search" in nav
    assert 'id="search-input"' in page
    assert "data-sort" in page
    assert '{ key: "proof", label: "Proof"' in page
    assert "£DP Index" in page
    assert "£FW Index" in page
    assert "supplier-slicer" in page
    assert "trait-filters" in page
    assert "pli_reliability" not in page
    assert 'label: "BC"' not in page
    assert 'label: "Supplier NI"' not in page
    assert '@router.get("/bull-search")' in routes
    assert '@router.post("/bull-search/refresh")' in routes
    assert '@router.put("/bull-search/index-settings")' in routes
    assert '@router.post("/bull-search/index-settings/reset")' in routes
    assert 'id="settings-toggle"' in page
    assert 'id="index-settings-form"' in page
    assert "Save indexes" in page
    assert "negative-pct" in page
    assert 'col.key === "fat_pct"' in page
    assert 'col.key === "protein_pct"' in page
    assert 'col.key === "fertility_index"' in page


def test_ensure_imported_skips_fetch_when_all_lists_populated() -> None:
    db = _session()
    now = dt.datetime(2026, 8, 1)
    for key in ("genomic", "proven"):
        db.add(AhdbBull(list_type=key, hbn=key, bull_name=key.upper(), fetched_at=now))
    db.commit()
    with patch("app.services.ahdb_bulls.fetch_reports") as fetch:
        result = ensure_imported(db)
    fetch.assert_not_called()
    assert result["count"] == 2
    db.close()


def test_ensure_imported_fetches_missing_lists() -> None:
    db = _session()
    db.add(
        AhdbBull(
            list_type="genomic",
            hbn="9",
            bull_name="EXISTING",
            fetched_at=dt.datetime(2026, 8, 1),
        )
    )
    db.commit()
    missing_report = AhdbReport(
        key="international",
        table="t",
        label="international",
        rows=[{"rank": 1, "bull_name": "INT BULL", "bull_hbn_emn": "88", "pli": 500}],
        columns=[],
        fetched_at=dt.datetime(2026, 8, 23),
    )
    with patch(
        "app.services.ahdb_bulls.fetch_reports",
        return_value=[missing_report],
    ) as fetch:
        result = ensure_imported(db)
    fetch.assert_called_once_with(keys=["proven", "international"])
    names = {row["bull_name"] for row in result["rows"]}
    assert "EXISTING" in names
    assert "INT BULL" in names
    db.close()
