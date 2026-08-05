"""CTWS RegBirths / RegMovs XML builder tests."""

from __future__ import annotations

from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from app.services.cts_submit_xml import (
    CtsSubmitXmlError,
    build_preview_xml,
    build_reg_births_xml,
    build_reg_movs_xml,
    filter_rows_for_kind,
)

_FAKE_CREDS = {
    "username": "256-271-732",
    "password": "secret-password",
    "holding": "55/013/0048",
}

_STAMP = "2026-08-04T14:00:00"


def _rows() -> list[dict]:
    return [
        {
            "id": "birth:CM:UK111:2026-08-01",
            "movement_type": "birth",
            "etag": "UK111111111111",
            "event_date": "2026-08-01",
            "dob": "2026-08-01",
            "sex": "F",
            "breed": "HF",
            "dreg": "UK222222222222",
            "sreg": "UK333333333333",
            "holding": "55/013/0048",
        },
        {
            "id": "sale:CM:UK444:2026-07-29",
            "movement_type": "sale",
            "etag": "UK444444444444",
            "event_date": "2026-07-29",
            "sex": "F",
            "breed": "HF",
            "holding": "55/013/0048",
        },
        {
            "id": "death:CM:UK555:2026-07-29",
            "movement_type": "death",
            "etag": "UK555555555555",
            "event_date": "2026-07-29",
            "sex": "M",
            "breed": "AAX",
            "holding": "55/013/0048",
        },
        {
            "id": "move_on:CM:UK666:2026-07-20",
            "movement_type": "move_on",
            "etag": "UK666666666666",
            "event_date": "2026-07-20",
            "sex": "F",
            "breed": "HEX",
            "holding": "55/013/0048",
        },
    ]


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_filter_rows_for_kind(_creds) -> None:
    rows = _rows()
    assert [r["movement_type"] for r in filter_rows_for_kind(rows, "births")] == ["birth"]
    assert [r["movement_type"] for r in filter_rows_for_kind(rows, "movements")] == [
        "sale",
        "death",
        "move_on",
    ]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_reg_births_xml_redacts_password_and_includes_fields(_creds) -> None:
    xml = build_reg_births_xml(_rows(), farm="CM", redact_password=True, request_timestamp=_STAMP)
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "secret-password" not in xml
    assert 'Pwd="***"' in xml
    assert 'Usr="256-271-732"' in xml
    root = ET.fromstring(xml.split("?>", 1)[-1])
    assert _local(root.tag) == "RegBirths"
    birth = next(el for el in root.iter() if _local(el.tag) == "Birth")
    assert birth.attrib["Etg"] == "UK111111111111"
    assert birth.attrib["Dob"] == "2026-08-01"
    assert birth.attrib["Sex"] == "f"
    assert birth.attrib["Brd"] == "HF"
    assert birth.attrib["GdEtg"] == "UK222222222222"
    assert birth.attrib["SIETG"] == "UK333333333333"
    assert birth.attrib["BLoc"] == "55/013/0048"
    assert birth.attrib["PLoc"] == "55/013/0048"
    assert birth.attrib["IWarn"] == "n"
    assert "DamEtg" not in birth.attrib
    assert "Loc" not in birth.attrib
    assert not any(_local(el.tag) == "Mov" for el in root.iter())


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_reg_movs_xml_mtypes(_creds) -> None:
    xml = build_reg_movs_xml(_rows(), farm="CM", redact_password=True, request_timestamp=_STAMP)
    assert "secret-password" not in xml
    assert 'Pwd="***"' in xml
    root = ET.fromstring(xml.split("?>", 1)[-1])
    assert _local(root.tag) == "RegMovs"
    movs = [el for el in root.iter() if _local(el.tag) == "Mov"]
    assert [m.attrib["MType"] for m in movs] == ["off", "death", "on"]
    assert [m.attrib["Etg"] for m in movs] == [
        "UK444444444444",
        "UK555555555555",
        "UK666666666666",
    ]
    assert not any(_local(el.tag) == "Birth" for el in root.iter())


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_build_preview_xml_filenames(_creds) -> None:
    name, xml = build_preview_xml(
        _rows(), farm="CM", kind="births", request_timestamp=_STAMP
    )
    assert name.startswith("RegBirths-CM-")
    assert name.endswith(".xml")
    assert "<RegBirths" in xml

    name, xml = build_preview_xml(
        _rows(), farm="CM", kind="movements", request_timestamp=_STAMP
    )
    assert name.startswith("RegMovs-CM-")
    assert "<RegMovs" in xml


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_empty_kind_raises(_creds) -> None:
    with pytest.raises(CtsSubmitXmlError, match="No birth rows"):
        build_reg_births_xml(
            [r for r in _rows() if r["movement_type"] != "birth"],
            farm="CM",
        )
    with pytest.raises(CtsSubmitXmlError, match="No sale/death/move-on"):
        build_reg_movs_xml(
            [r for r in _rows() if r["movement_type"] == "birth"],
            farm="CM",
        )


@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=None)
def test_missing_credentials_raises(_creds) -> None:
    with pytest.raises(CtsSubmitXmlError, match="not configured"):
        build_reg_births_xml(_rows(), farm="CM")
