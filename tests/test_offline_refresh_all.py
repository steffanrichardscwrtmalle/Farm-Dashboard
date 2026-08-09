"""Offline refresh-all orchestration (every cron-equivalent job)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.offline_refresh_all import refresh_all_cron_jobs


def test_refresh_all_runs_each_cron_step() -> None:
    db = MagicMock()
    with (
        patch(
            "app.services.offline_refresh_all.import_haulier_collections",
            return_value={"rows_total": 1},
        ) as haulier,
        patch(
            "app.services.offline_refresh_all.import_nml_results",
            return_value={"rows_total": 2},
        ) as nml,
        patch(
            "app.services.offline_refresh_all.import_milk_statements",
            return_value={"rows_total": 3},
        ) as statements,
        patch(
            "app.services.offline_refresh_all.import_cattle_sales",
            return_value={"rows_total": 4},
        ) as sales,
        patch(
            "app.services.offline_refresh_all.import_parlour_milk_flow",
            return_value={"rows_total": 5},
        ) as parlour,
        patch(
            "app.services.offline_refresh_all.refresh_herd_from_onedrive",
            return_value={"ok": True, "events": {"rows_imported": 9}},
        ) as herd,
        patch(
            "app.services.offline_refresh_all.sync_farms",
            return_value={"farms": []},
        ) as cts,
    ):
        result = refresh_all_cron_jobs(db, days=3)

    haulier.assert_called_once_with(db, days=3)
    nml.assert_called_once_with(db, days=3)
    statements.assert_called_once_with(db, days=3)
    sales.assert_called_once_with(db, days=3)
    parlour.assert_called_once_with(db, days=3)
    herd.assert_called_once_with(db, include_genomics=True)
    cts.assert_called_once_with(db, source="offline-refresh")
    assert result["ok"] is True
    assert result["failures"] == []
    assert result["steps"]["haulier"]["rows_total"] == 1


def test_refresh_all_continues_after_step_failure() -> None:
    db = MagicMock()
    with (
        patch(
            "app.services.offline_refresh_all.import_haulier_collections",
            side_effect=ValueError("mail down"),
        ),
        patch(
            "app.services.offline_refresh_all.import_nml_results",
            return_value={"rows_total": 1},
        ),
        patch(
            "app.services.offline_refresh_all.import_milk_statements",
            return_value={"rows_total": 0},
        ),
        patch(
            "app.services.offline_refresh_all.import_cattle_sales",
            return_value={"rows_total": 0},
        ),
        patch(
            "app.services.offline_refresh_all.import_parlour_milk_flow",
            return_value={"rows_total": 0},
        ),
        patch(
            "app.services.offline_refresh_all.refresh_herd_from_onedrive",
            return_value={"ok": True},
        ),
        patch(
            "app.services.offline_refresh_all.sync_farms",
            return_value={"farms": []},
        ),
    ):
        result = refresh_all_cron_jobs(db, days=2)

    assert result["ok"] is False
    assert any("haulier" in f for f in result["failures"])
    assert result["steps"]["nml"]["rows_total"] == 1
    assert "error" in result["steps"]["haulier"]
