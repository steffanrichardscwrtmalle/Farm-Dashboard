"""Cow-event date parsing must accept 2-digit and 4-digit DC305 dates."""

from __future__ import annotations

import pandas as pd

from app.services.herd_events_import import _clean_events_dataframe
from app.services.herd_import_utils import parse_date_series


def test_parse_date_series_accepts_two_and_four_digit_years() -> None:
    series = pd.Series(["16/08/26", "16/08/2026", "18/04/2022", "17/12/22", "", None, "not-a-date"])
    parsed = parse_date_series(series)
    assert parsed.iloc[0] == pd.Timestamp("2026-08-16")
    assert parsed.iloc[1] == pd.Timestamp("2026-08-16")
    assert parsed.iloc[2] == pd.Timestamp("2022-04-18")
    assert parsed.iloc[3] == pd.Timestamp("2022-12-17")
    assert pd.isna(parsed.iloc[4])
    assert pd.isna(parsed.iloc[5])
    assert pd.isna(parsed.iloc[6])


def test_clean_events_keeps_four_digit_event_dates() -> None:
    df = pd.DataFrame(
        {
            "ID": ["1"],
            "ETAG": ["UK322300101307"],
            "BDAT": ["28/12/2013"],
            "FDAT": ["19/02/2021"],
            "LACT": [6],
            "EDAT": ["31/10/2019"],
            "Event": ["SOLD"],
            "Date": ["12/05/2022"],
            "Remark": ["LOWYIELD"],
            "Farm": ["CM"],
        }
    )
    cleaned = _clean_events_dataframe(df)
    assert cleaned["Date"].iloc[0] == pd.Timestamp("2022-05-12")
    assert cleaned["Event"].iloc[0] == "SOLD"
