"""Tests for full OneDrive herd refresh orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.herd_full_import import refresh_herd_from_onedrive


def test_refresh_requires_graph_or_local_dir():
    db = MagicMock()
    with patch("app.services.herd_full_import.graph_is_configured", return_value=False):
        with pytest.raises(ValueError, match="not configured"):
            refresh_herd_from_onedrive(db)


def test_refresh_runs_all_steps_including_genomics():
    db = MagicMock()
    with (
        patch("app.services.herd_full_import.graph_is_configured", return_value=True),
        patch(
            "app.services.herd_full_import.import_cow_events",
            return_value={
                "rows_imported": 10,
                "farm_counts": {"CM": 6, "GAD": 4},
                "latest_event_date": "2026-07-01",
            },
        ) as events,
        patch(
            "app.services.herd_full_import.import_herd_inventory",
            return_value={"rows_imported": 5, "farm_counts": {"CM": 3}},
        ) as inventory,
        patch(
            "app.services.herd_full_import.import_herd_births",
            return_value={
                "rows_imported": 2,
                "farm_counts": {"CM": 2},
                "latest_birth_date": "2026-07-02",
            },
        ) as births,
        patch(
            "app.services.herd_full_import.rebuild_stock_valuation_snapshots",
            return_value={"rows_written": 8, "anchor_import_timestamp": None},
        ) as valuations,
        patch(
            "app.services.herd_full_import.rebuild_stock_accrual_snapshots",
            return_value={"rows_written": 7, "anchor_import_timestamp": None},
        ) as accruals,
        patch(
            "app.services.herd_full_import.import_genomic_results",
            return_value={"rows_imported": 3, "skipped": False},
        ) as genomics,
    ):
        result = refresh_herd_from_onedrive(db, include_genomics=True)

    events.assert_called_once_with(db)
    inventory.assert_called_once_with(db)
    births.assert_called_once_with(db)
    valuations.assert_called_once_with(db)
    accruals.assert_called_once_with(db)
    genomics.assert_called_once_with(db, force=True)

    assert result["ok"] is True
    assert result["events"]["rows_imported"] == 10
    assert result["inventory"]["rows_imported"] == 5
    assert result["births"]["rows_imported"] == 2
    assert result["genomics"]["rows_imported"] == 3


def test_refresh_can_skip_genomics():
    db = MagicMock()
    with (
        patch("app.services.herd_full_import.graph_is_configured", return_value=True),
        patch(
            "app.services.herd_full_import.import_cow_events",
            return_value={"rows_imported": 1, "farm_counts": {}, "latest_event_date": None},
        ),
        patch(
            "app.services.herd_full_import.import_herd_inventory",
            return_value={"rows_imported": 1, "farm_counts": {}},
        ),
        patch(
            "app.services.herd_full_import.import_herd_births",
            return_value={
                "rows_imported": 1,
                "farm_counts": {},
                "latest_birth_date": None,
            },
        ),
        patch(
            "app.services.herd_full_import.rebuild_stock_valuation_snapshots",
            return_value={"rows_written": 1, "anchor_import_timestamp": None},
        ),
        patch(
            "app.services.herd_full_import.rebuild_stock_accrual_snapshots",
            return_value={"rows_written": 1, "anchor_import_timestamp": None},
        ),
        patch("app.services.herd_full_import.import_genomic_results") as genomics,
    ):
        result = refresh_herd_from_onedrive(db, include_genomics=False)

    genomics.assert_not_called()
    assert result["genomics"] is None
