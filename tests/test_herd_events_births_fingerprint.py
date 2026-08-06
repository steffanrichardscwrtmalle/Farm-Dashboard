"""Fingerprint skip-when-unchanged for cow events and births imports."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.herd_birth_import import (
    CM_BIRTH_FILE,
    GAD_BIRTH_FILE,
    import_herd_births,
)
from app.services.herd_events_import import (
    CM_EVENTS_FILE,
    GAD_EVENTS_FILE,
    import_cow_events,
)
from app.services.herd_import_utils import source_fingerprint


def _metas(cm_path: str, gad_path: str, cm_mtime: str, gad_mtime: str):
    def side_effect(path: str) -> dict[str, str]:
        if path == cm_path:
            return {
                "relative_path": cm_path,
                "name": cm_path.rsplit("/", 1)[-1],
                "last_modified": cm_mtime,
            }
        return {
            "relative_path": gad_path,
            "name": gad_path.rsplit("/", 1)[-1],
            "last_modified": gad_mtime,
        }

    return side_effect


def test_events_import_skips_both_when_unchanged() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 100), ("GAD", 200)]
    db.scalar.return_value = None
    cm_fp = source_fingerprint(CM_EVENTS_FILE, "2026-08-05T06:00:00Z")
    gad_fp = source_fingerprint(GAD_EVENTS_FILE, "2026-08-05T06:05:00Z")

    with (
        patch(
            "app.services.herd_events_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_events_import.herd_file_meta",
            side_effect=_metas(
                CM_EVENTS_FILE,
                GAD_EVENTS_FILE,
                "2026-08-05T06:00:00Z",
                "2026-08-05T06:05:00Z",
            ),
        ),
        patch(
            "app.services.herd_events_import.load_source_fingerprint",
            side_effect=lambda _db, _prefix, farm: cm_fp if farm == "CM" else gad_fp,
        ),
        patch("app.services.herd_events_import._import_farm_file") as import_file,
    ):
        result = import_cow_events(db, force=False)

    assert result["skipped"] is True
    assert result["farms_imported"] == []
    assert result["farms_skipped"] == ["CM", "GAD"]
    import_file.assert_not_called()
    db.commit.assert_not_called()


def test_events_import_only_changed_farm() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 50), ("GAD", 200)]
    db.scalar.return_value = None
    cm_old = source_fingerprint(CM_EVENTS_FILE, "2026-08-05T06:00:00Z")
    gad_fp = source_fingerprint(GAD_EVENTS_FILE, "2026-08-05T06:05:00Z")

    with (
        patch(
            "app.services.herd_events_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_events_import.herd_file_meta",
            side_effect=_metas(
                CM_EVENTS_FILE,
                GAD_EVENTS_FILE,
                "2026-08-05T07:00:00Z",
                "2026-08-05T06:05:00Z",
            ),
        ),
        patch(
            "app.services.herd_events_import.load_source_fingerprint",
            side_effect=lambda _db, _prefix, farm: cm_old if farm == "CM" else gad_fp,
        ),
        patch(
            "app.services.herd_events_import._import_farm_file",
            return_value=50,
        ) as import_file,
        patch("app.services.herd_events_import.store_source_fingerprint") as store_fp,
        patch(
            "app.services.herd_events_import.remove_duplicate_fresh_cow_events",
            return_value=0,
        ),
        patch(
            "app.services.herd_events_import.remove_duplicate_exit_cow_events",
            return_value=0,
        ),
        patch(
            "app.services.herd_events_import.rebuild_stock_purchases",
            return_value={},
        ),
    ):
        result = import_cow_events(db, force=False)

    assert result["skipped"] is False
    assert result["farms_imported"] == ["CM"]
    assert result["farms_skipped"] == ["GAD"]
    assert result["rows_imported"] == 50
    import_file.assert_called_once()
    assert store_fp.call_args[0][2] == "CM"
    db.commit.assert_called_once()


def test_births_import_skips_both_when_unchanged() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 10), ("GAD", 12)]
    db.scalar.return_value = None
    cm_fp = source_fingerprint(CM_BIRTH_FILE, "2026-08-05T06:00:00Z")
    gad_fp = source_fingerprint(GAD_BIRTH_FILE, "2026-08-05T06:05:00Z")

    with (
        patch(
            "app.services.herd_birth_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_birth_import.herd_file_meta",
            side_effect=_metas(
                CM_BIRTH_FILE,
                GAD_BIRTH_FILE,
                "2026-08-05T06:00:00Z",
                "2026-08-05T06:05:00Z",
            ),
        ),
        patch(
            "app.services.herd_birth_import.load_source_fingerprint",
            side_effect=lambda _db, _prefix, farm: cm_fp if farm == "CM" else gad_fp,
        ),
        patch("app.services.herd_birth_import._import_farm_file") as import_file,
    ):
        result = import_herd_births(db, force=False)

    assert result["skipped"] is True
    assert result["farms_imported"] == []
    assert result["farms_skipped"] == ["CM", "GAD"]
    import_file.assert_not_called()
    db.commit.assert_not_called()


def test_births_import_force_reloads_both() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 10), ("GAD", 12)]
    db.scalar.return_value = None

    with (
        patch(
            "app.services.herd_birth_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_birth_import.herd_file_meta",
            side_effect=_metas(
                CM_BIRTH_FILE,
                GAD_BIRTH_FILE,
                "2026-08-05T06:00:00Z",
                "2026-08-05T06:05:00Z",
            ),
        ),
        patch(
            "app.services.herd_birth_import.load_source_fingerprint",
            return_value="unchanged",
        ),
        patch(
            "app.services.herd_birth_import._import_farm_file",
            side_effect=[(10, 0), (12, 1)],
        ) as import_file,
        patch("app.services.herd_birth_import.store_source_fingerprint"),
    ):
        result = import_herd_births(db, force=True)

    assert result["skipped"] is False
    assert result["farms_imported"] == ["CM", "GAD"]
    assert result["rows_imported"] == 22
    assert result["duplicate_rows_dropped"] == 1
    assert import_file.call_count == 2
