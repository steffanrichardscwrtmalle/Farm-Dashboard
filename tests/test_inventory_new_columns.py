"""Inventory CSV processing for new DairyComp columns."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.services.herd_inventory_import import _dataframe_to_mappings
from app.services.inventory_processor import process_inventory_file


def test_inventory_import_maps_ewgt_httag_rum_pen_tbrd() -> None:
    source = pd.DataFrame(
        [
            {
                "id": "101",
                "etag": "UK740651324400     ",
                "lact": 0,
                "sbrd": "HF",
                "rc": 3,
                "ewgt": 385.5,
                "httag": 12.0,
                "rum": 520,
                "pen": 8.0,
                "tbrd": 2,
                "remark": "BRED",
            },
            {
                "id": "102",
                "etag": "UK740651300111",
                "lact": 0,
                "sbrd": "HF",
                "rc": 4,
                "ewgt": "",
                "httag": "  45  ",
                "rum": "",
                "pen": "H1",
                "tbrd": 0,
                "remark": "",
            },
            {"id": "TOTAL"},
        ]
    )

    processed = process_inventory_file(source, "CM")
    assert list(processed["PEN"]) == ["8", "H1"]
    assert list(processed["HTTAG"]) == ["12", "45"]
    assert list(processed["TBRD"]) == [2, 0]
    assert processed.iloc[0]["EWGT"] == 385.5
    assert pd.isna(processed.iloc[1]["EWGT"])
    assert processed.iloc[0]["RUM"] == 520
    assert processed.iloc[0]["ETAG"] == "UK740651324400"

    rows = _dataframe_to_mappings(processed, dt.datetime(2026, 8, 16, 12, 0))
    assert rows[0]["ewgt"] == 385.5
    assert rows[0]["httag"] == "12"
    assert rows[0]["rum"] == 520
    assert rows[0]["pen"] == "8"
    assert rows[0]["tbrd"] == 2
    assert rows[1]["pen"] == "H1"
    assert rows[1]["httag"] == "45"
    assert rows[1]["tbrd"] == 0
    assert pd.isna(rows[1]["ewgt"]) or rows[1]["ewgt"] is None
    assert pd.isna(rows[1]["rum"]) or rows[1]["rum"] is None


def test_inventory_headers_are_stripped_and_uppercased() -> None:
    source = pd.DataFrame(
        [
            {
                " pen ": 12,
                "Tbrd": 1,
                "Ewgt": 400,
                "id": "1",
                "lact": 0,
                "sbrd": "HF",
                "rc": 3,
            },
            {"id": "TOTAL"},
        ]
    )
    processed = process_inventory_file(source, "GAD")
    assert processed.iloc[0]["PEN"] == "12"
    assert processed.iloc[0]["TBRD"] == 1
    assert processed.iloc[0]["EWGT"] == 400
