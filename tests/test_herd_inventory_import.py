"""Tests for herd inventory import skip-when-unchanged behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.herd_inventory_import import (
    CM_INVENTORY_FILE,
    GAD_INVENTORY_FILE,
    import_herd_inventory,
)


def test_inventory_import_skips_when_fingerprint_unchanged() -> None:
    db = MagicMock()
    db.scalar.return_value = 42
    db.execute.return_value.all.return_value = [("CM", 20), ("GAD", 22)]

    with (
        patch(
            "app.services.herd_inventory_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_inventory_import._inventory_source_metas",
            return_value=[
                {
                    "source_file": CM_INVENTORY_FILE,
                    "last_modified": "2026-08-05T06:00:00Z",
                },
                {
                    "source_file": GAD_INVENTORY_FILE,
                    "last_modified": "2026-08-05T06:05:00Z",
                },
            ],
        ),
        patch(
            "app.services.herd_inventory_import._fingerprint_sources",
            return_value="same-fingerprint",
        ),
        patch(
            "app.services.herd_inventory_import._load_stored_fingerprint",
            return_value="same-fingerprint",
        ),
        patch(
            "app.services.herd_inventory_import._import_farm_file"
        ) as import_file,
    ):
        result = import_herd_inventory(db, force=False)

    assert result["skipped"] is True
    assert result["reason"] == "source_unchanged"
    assert result["rows_imported"] == 42
    import_file.assert_not_called()


def test_inventory_import_runs_when_force_true_even_if_unchanged() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = [("CM", 10), ("GAD", 20)]

    with (
        patch(
            "app.services.herd_inventory_import.graph_is_configured",
            return_value=True,
        ),
        patch(
            "app.services.herd_inventory_import._inventory_source_metas",
            return_value=[
                {
                    "source_file": CM_INVENTORY_FILE,
                    "last_modified": "2026-08-05T06:00:00Z",
                },
                {
                    "source_file": GAD_INVENTORY_FILE,
                    "last_modified": "2026-08-05T06:05:00Z",
                },
            ],
        ),
        patch(
            "app.services.herd_inventory_import._load_stored_fingerprint",
            return_value="same-fingerprint",
        ),
        patch(
            "app.services.herd_inventory_import._fingerprint_sources",
            return_value="same-fingerprint",
        ),
        patch(
            "app.services.herd_inventory_import._import_farm_file",
            side_effect=[10, 20],
        ) as import_file,
        patch(
            "app.services.herd_inventory_import._sync_pedigree_records",
            return_value=5,
        ),
        patch("app.services.herd_inventory_import._store_fingerprint"),
    ):
        result = import_herd_inventory(db, force=True)

    assert result.get("skipped") is False
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
