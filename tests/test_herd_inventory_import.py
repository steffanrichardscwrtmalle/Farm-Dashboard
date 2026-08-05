"""Tests for per-farm herd inventory import skip-when-unchanged behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.herd_inventory_import import (
    CM_INVENTORY_FILE,
    GAD_INVENTORY_FILE,
    _farm_fingerprint,
    import_herd_inventory,
)


def _metas(cm_mtime: str, gad_mtime: str):
    def side_effect(path: str) -> dict[str, str]:
        if path == CM_INVENTORY_FILE:
            return {
                "relative_path": CM_INVENTORY_FILE,
                "name": "CMINV.CSV",
                "last_modified": cm_mtime,
            }
        return {
            "relative_path": GAD_INVENTORY_FILE,
            "name": "GADINV.CSV",
            "last_modified": gad_mtime,
        }

    return side_effect


def test_inventory_import_skips_both_when_unchanged() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 20), ("GAD", 22)]
    cm_fp = _farm_fingerprint(CM_INVENTORY_FILE, "2026-08-05T06:00:00Z")
    gad_fp = _farm_fingerprint(GAD_INVENTORY_FILE, "2026-08-05T06:05:00Z")

    with (
        patch(
            "app.services.herd_inventory_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_inventory_import.herd_file_meta",
            side_effect=_metas("2026-08-05T06:00:00Z", "2026-08-05T06:05:00Z"),
        ),
        patch(
            "app.services.herd_inventory_import._load_farm_fingerprint",
            side_effect=lambda _db, farm: cm_fp if farm == "CM" else gad_fp,
        ),
        patch(
            "app.services.herd_inventory_import._import_farm_file"
        ) as import_file,
    ):
        result = import_herd_inventory(db, force=False)

    assert result["skipped"] is True
    assert result["farms_imported"] == []
    assert result["farms_skipped"] == ["CM", "GAD"]
    import_file.assert_not_called()
    db.commit.assert_not_called()


def test_inventory_import_only_changed_farm() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 10), ("GAD", 20)]
    cm_old = _farm_fingerprint(CM_INVENTORY_FILE, "2026-08-05T06:00:00Z")
    gad_fp = _farm_fingerprint(GAD_INVENTORY_FILE, "2026-08-05T06:05:00Z")

    with (
        patch(
            "app.services.herd_inventory_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_inventory_import.herd_file_meta",
            side_effect=_metas("2026-08-05T07:00:00Z", "2026-08-05T06:05:00Z"),
        ),
        patch(
            "app.services.herd_inventory_import._load_farm_fingerprint",
            side_effect=lambda _db, farm: cm_old if farm == "CM" else gad_fp,
        ),
        patch(
            "app.services.herd_inventory_import._import_farm_file",
            return_value=10,
        ) as import_file,
        patch(
            "app.services.herd_inventory_import._sync_pedigree_records",
            return_value=3,
        ) as sync_ped,
        patch(
            "app.services.herd_inventory_import._store_farm_fingerprint"
        ) as store_fp,
    ):
        result = import_herd_inventory(db, force=False)

    assert result["skipped"] is False
    assert result["farms_imported"] == ["CM"]
    assert result["farms_skipped"] == ["GAD"]
    assert result["rows_imported"] == 10
    import_file.assert_called_once()
    assert import_file.call_args[0][1] == CM_INVENTORY_FILE
    sync_ped.assert_called_once_with(db, farm="CM")
    assert store_fp.call_args[0][1] == "CM"
    db.commit.assert_called_once()


def test_inventory_import_force_reloads_both() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 10), ("GAD", 20)]

    with (
        patch(
            "app.services.herd_inventory_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_inventory_import.herd_file_meta",
            side_effect=_metas("2026-08-05T06:00:00Z", "2026-08-05T06:05:00Z"),
        ),
        patch(
            "app.services.herd_inventory_import._load_farm_fingerprint",
            return_value="unchanged",
        ),
        patch(
            "app.services.herd_inventory_import._import_farm_file",
            side_effect=[10, 20],
        ) as import_file,
        patch(
            "app.services.herd_inventory_import._sync_pedigree_records",
            return_value=2,
        ),
        patch("app.services.herd_inventory_import._store_farm_fingerprint"),
    ):
        result = import_herd_inventory(db, force=True)

    assert result["skipped"] is False
    assert result["farms_imported"] == ["CM", "GAD"]
    assert result["farms_skipped"] == []
    assert result["rows_imported"] == 30
    assert import_file.call_count == 2


def test_inventory_import_requires_graph_or_local_dir() -> None:
    db = MagicMock()
    with patch(
        "app.services.herd_inventory_import.graph_is_configured",
        return_value=False,
    ):
        with pytest.raises(ValueError, match="not configured"):
            import_herd_inventory(db)
