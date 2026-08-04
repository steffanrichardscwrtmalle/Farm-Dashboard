"""Preserve original DairyComp DIED/SOLD for BCMS."""

from __future__ import annotations

import pandas as pd

from app.services.herd_events_import import _clean_events_dataframe


def test_clean_events_keeps_died_tb_and_ofs() -> None:
    df = pd.DataFrame(
        {
            "Event": ["DIED", "DIED", "SOLD", "DIED"],
            "Remark": ["TB", "OFS", "TB", "NATURAL"],
            "FDAT": [pd.NaT, pd.NaT, pd.NaT, pd.NaT],
            "Date": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
            ),
            "EDAT": pd.to_datetime(
                ["2025-01-01", "2025-01-01", "2025-01-01", "2025-01-01"]
            ),
            "LACT": [1, 1, 1, 1],
            "Farm": ["CM", "CM", "CM", "CM"],
            "ETAG": ["UK1", "UK2", "UK3", "UK4"],
            "ID": ["1", "2", "3", "4"],
        }
    )
    out = _clean_events_dataframe(df)
    by_etag = {row.ETAG: row.Event for row in out.itertuples()}
    assert by_etag["UK1"] == "DIED"
    assert by_etag["UK2"] == "DIED"
    assert by_etag["UK3"] == "SOLD"
    assert by_etag["UK4"] == "DIED"
