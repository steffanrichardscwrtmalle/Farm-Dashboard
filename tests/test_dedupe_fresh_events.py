"""Tests for duplicate FRESH event deduplication."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.services.herd_import_utils import dedupe_fresh_event_rows


def test_dedupe_fresh_event_rows_keeps_first_occurrence() -> None:
    df = pd.DataFrame(
        {
            "Farm": ["GAD", "GAD", "GAD"],
            "ETAG": ["UK752261110015", "UK752261110015", "UK752261110016"],
            "ID": ["110015", "110015", "110016"],
            "Date": pd.to_datetime(["2025-01-04", "2025-01-04", "2025-01-05"]),
            "LACT": [1, 1, 1],
            "Event": ["FRESH", "FRESH", "FRESH"],
        }
    )
    out, dropped = dedupe_fresh_event_rows(df)
    assert dropped == 1
    assert len(out) == 2
    assert out[out["Event"] == "FRESH"]["ETAG"].tolist() == ["UK752261110015", "UK752261110016"]


def test_dedupe_fresh_event_rows_leaves_other_events() -> None:
    df = pd.DataFrame(
        {
            "Farm": ["GAD", "GAD"],
            "ETAG": ["UK1", "UK1"],
            "ID": ["1", "1"],
            "Date": pd.to_datetime(["2025-01-04", "2025-01-04"]),
            "LACT": [1, 1],
            "Event": ["FRESH", "BRED"],
        }
    )
    out, dropped = dedupe_fresh_event_rows(df)
    assert dropped == 0
    assert len(out) == 2
