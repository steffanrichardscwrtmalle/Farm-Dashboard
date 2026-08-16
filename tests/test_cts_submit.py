"""Live BCMS submit flow tests (DDTS mocked)."""

from __future__ import annotations

from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services.cts_submit import (
    CtsSubmitError,
    _parse_receipt,
    _parse_validation_results,
    send_deadline_day_movements,
    send_pending_movements,
)

_FAKE_CREDS = {
    "username": "256-271-732",
    "password": "secret-password",
    "holding": "55/013/0048",
}


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _birth_row() -> dict:
    return {
        "id": "birth:CM:UK111:2026-08-01",
        "movement_type": "birth",
        "etag": "UK111111111111",
        "event_date": "2026-08-01",
        "dob": "2026-08-01",
        "sex": "F",
        "breed": "HF",
        "dreg": "UK222222222222",
        "holding": "55/013/0048",
    }


def _sale_row() -> dict:
    return {
        "id": "sale:CM:UK444:2026-07-29",
        "movement_type": "sale",
        "etag": "UK444444444444",
        "event_date": "2026-07-29",
        "holding": "55/013/0048",
    }


def test_parse_receipt() -> None:
    root = ET.fromstring(
        '<AsyncReceipt xmlns="http://defra.bcms.ctws/asynchronous_receipt">'
        '<Receipt Num="43629922"/>'
        "</AsyncReceipt>"
    )
    assert _parse_receipt(root) == "43629922"


def test_parse_validation_accept_reject_by_etag() -> None:
    ns = "http://defra.bcms.ctws/register_movements_request_results"
    root = ET.fromstring(
        f'<Results xmlns="{ns}">'
        f'<Accept><Mov RowNum="1" Etg="UK444444444444"/></Accept>'
        f'<Reject><Mov RowNum="2" Etg="UK555555555555"/>'
        f'<Cause Desc="Animal not on holding"/></Reject>'
        f"</Results>"
    )
    rows = [
        _sale_row(),
        {
            "id": "sale:CM:UK555:2026-07-30",
            "movement_type": "sale",
            "etag": "UK555555555555",
            "event_date": "2026-07-30",
        },
    ]
    parsed = _parse_validation_results(root, kind="movements", rows=rows)
    assert parsed["accepted_count"] == 1
    assert parsed["rejected_count"] == 1
    assert parsed["accepted"][0]["etag"] == "UK444444444444"
    assert "Animal not on holding" in parsed["rejected"][0]["reasons"]


def test_parse_birth_accept_with_etag_on_accept_node() -> None:
    ns = "http://defra.bcms.ctws/register_births_request_results"
    root = ET.fromstring(
        f'<Results xmlns="{ns}">'
        f'<Accept RowNum="1" Etg="UK111111111111"/>'
        f"</Results>"
    )
    parsed = _parse_validation_results(
        root, kind="births", rows=[_birth_row()]
    )
    assert parsed["accepted_count"] == 1
    assert parsed["accepted"][0]["etag"] == "UK111111111111"


@patch("app.services.cts_submit.transfer_ctws")
@patch("app.services.cts_submit.cts_farm_credentials", return_value=_FAKE_CREDS)
@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_send_pending_waits_on_empty_results(
    _creds_xml, _creds_submit, mock_transfer
) -> None:
    receipt_ns = "http://defra.bcms.ctws/asynchronous_receipt"
    birth_ns = "http://defra.bcms.ctws/register_births_request_results"
    empty = ET.fromstring(f'<Results xmlns="{birth_ns}"/>')
    done = ET.fromstring(
        f'<Results xmlns="{birth_ns}">'
        f'<Accept><Birth RowNum="1" Etg="UK111111111111"/></Accept>'
        f"</Results>"
    )
    receipt = ET.fromstring(
        f'<AsyncReceipt xmlns="{receipt_ns}"><Receipt Num="12"/></AsyncReceipt>'
    )
    calls = {"n": 0}

    def _side_effect(kind: str, request: ET.Element, **kwargs):
        if kind.startswith("Register_"):
            return receipt
        calls["n"] += 1
        return empty if calls["n"] == 1 else done

    mock_transfer.side_effect = _side_effect
    session = _session()
    result = send_pending_movements(
        session,
        farm="CM",
        rows=[_birth_row()],
        poll_attempts=5,
        poll_sleep_s=0,
    )
    assert result["accepted_count"] == 1
    assert calls["n"] == 2


@patch("app.services.cts_submit.transfer_ctws")
@patch("app.services.cts_submit.cts_farm_credentials", return_value=_FAKE_CREDS)
@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_send_pending_movements_marks_accepted(
    _creds_xml, _creds_submit, mock_transfer
) -> None:
    receipt_ns = "http://defra.bcms.ctws/asynchronous_receipt"
    mov_ns = "http://defra.bcms.ctws/register_movements_request_results"
    receipt_xml = (
        f'<AsyncReceipt xmlns="{receipt_ns}"><Receipt Num="99"/></AsyncReceipt>'
    )
    results_xml = (
        f'<Results xmlns="{mov_ns}">'
        f'<Accept><Mov RowNum="1" Etg="UK444444444444"/></Accept>'
        f"</Results>"
    )

    def _side_effect(kind: str, request: ET.Element, **kwargs):
        if kind.startswith("Register_"):
            return ET.fromstring(receipt_xml)
        if kind.startswith("Get_Register_"):
            return ET.fromstring(results_xml)
        raise AssertionError(f"Unexpected kind {kind}")

    mock_transfer.side_effect = _side_effect
    session = _session()
    result = send_pending_movements(
        session,
        farm="CM",
        rows=[_sale_row()],
        poll_attempts=3,
        poll_sleep_s=0,
    )
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 0
    assert result["receipts"] == ["99"]
    assert "accepted" in result["message"].lower()


@patch("app.services.cts_submit.transfer_ctws")
@patch("app.services.cts_submit.cts_farm_credentials", return_value=_FAKE_CREDS)
@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_send_pending_waits_for_ctws806(
    _creds_xml, _creds_submit, mock_transfer
) -> None:
    receipt_ns = "http://defra.bcms.ctws/asynchronous_receipt"
    birth_ns = "http://defra.bcms.ctws/register_births_request_results"
    pending = ET.fromstring(
        '<GetResults><SystemException ExNum="CTWS806" ExMsg="Pending"/></GetResults>'
    )
    receipt = ET.fromstring(
        f'<AsyncReceipt xmlns="{receipt_ns}"><Receipt Num="12"/></AsyncReceipt>'
    )
    done = ET.fromstring(
        f'<Results xmlns="{birth_ns}">'
        f'<Accept><Birth RowNum="1" Etg="UK111111111111"/></Accept>'
        f"</Results>"
    )
    calls = {"n": 0}

    def _side_effect(kind: str, request: ET.Element, **kwargs):
        if kind.startswith("Register_"):
            return receipt
        calls["n"] += 1
        if calls["n"] == 1:
            return pending
        return done

    mock_transfer.side_effect = _side_effect
    session = _session()
    result = send_pending_movements(
        session,
        farm="CM",
        rows=[_birth_row()],
        poll_attempts=5,
        poll_sleep_s=0,
    )
    assert result["accepted_count"] == 1
    assert calls["n"] == 2


@patch("app.services.cts_submit.transfer_ctws")
@patch("app.services.cts_submit.cts_farm_credentials", return_value=_FAKE_CREDS)
@patch("app.services.cts_submit_xml.cts_farm_credentials", return_value=_FAKE_CREDS)
def test_send_mixed_submits_two_batches(
    _creds_xml, _creds_submit, mock_transfer
) -> None:
    receipt_ns = "http://defra.bcms.ctws/asynchronous_receipt"
    birth_ns = "http://defra.bcms.ctws/register_births_request_results"
    mov_ns = "http://defra.bcms.ctws/register_movements_request_results"
    kinds: list[str] = []

    def _side_effect(kind: str, request: ET.Element, **kwargs):
        kinds.append(kind)
        if kind.startswith("Register_"):
            return ET.fromstring(
                f'<AsyncReceipt xmlns="{receipt_ns}"><Receipt Num="1"/></AsyncReceipt>'
            )
        if "Births" in kind:
            return ET.fromstring(
                f'<Results xmlns="{birth_ns}">'
                f'<Accept><Birth RowNum="1" Etg="UK111111111111"/></Accept>'
                f"</Results>"
            )
        return ET.fromstring(
            f'<Results xmlns="{mov_ns}">'
            f'<Accept><Mov RowNum="1" Etg="UK444444444444"/></Accept>'
            f"</Results>"
        )

    mock_transfer.side_effect = _side_effect
    session = _session()
    result = send_pending_movements(
        session,
        farm="CM",
        rows=[_birth_row(), _sale_row()],
        poll_attempts=3,
        poll_sleep_s=0,
    )
    assert result["accepted_count"] == 2
    assert "Register_Births_Asynchronous-V1-0" in kinds
    assert "Register_Movements_Asynchronous-V1-0" in kinds


def test_send_empty_raises() -> None:
    session = _session()
    with pytest.raises(CtsSubmitError, match="No pending"):
        send_pending_movements(session, farm="CM", rows=[])


@patch("app.services.cts_submit.cts_ddts_is_configured", return_value=True)
@patch("app.services.cts_submit.cts_configured_farms", return_value=["CM"])
@patch("app.services.cts_submit.send_pending_movements")
@patch("app.services.cts_submit.list_pending_movements")
def test_send_deadline_day_only_sends_exact_deadline(
    mock_pending, mock_send, _ready, _ddts
) -> None:
    mock_pending.return_value = {
        "rows": [
            {"id": "sale:CM:A", "movement_type": "sale", "days_since_event": 3},
            {"id": "sale:CM:B", "movement_type": "sale", "days_since_event": 4},
            {"id": "birth:CM:C", "movement_type": "birth", "days_since_event": 16},
            {"id": "birth:CM:D", "movement_type": "birth", "days_since_event": 17},
            {"id": "death:CM:E", "movement_type": "death", "days_since_event": 6},
            {"id": "death:CM:F", "movement_type": "death", "days_since_event": 7},
            {"id": "move_on:CM:G", "movement_type": "move_on", "days_since_event": 3},
        ]
    }
    mock_send.return_value = {
        "ok": True,
        "accepted_count": 4,
        "rejected_count": 0,
        "message": "Sent.",
    }
    result = send_deadline_day_movements(_session())
    assert result["ok"] is True
    sent_rows = mock_send.call_args.kwargs["rows"]
    assert {row["id"] for row in sent_rows} == {
        "sale:CM:A",
        "birth:CM:D",
        "death:CM:F",
        "move_on:CM:G",
    }
    farm = result["results"][0]
    assert farm["due_count"] == 4
    assert farm["accepted_count"] == 4


@patch("app.services.cts_submit.cts_ddts_is_configured", return_value=True)
@patch("app.services.cts_submit.cts_configured_farms", return_value=["CM"])
@patch("app.services.cts_submit.send_pending_movements")
@patch("app.services.cts_submit.list_pending_movements")
def test_send_deadline_day_skips_when_none_due(
    mock_pending, mock_send, _ready, _ddts
) -> None:
    mock_pending.return_value = {
        "rows": [
            {"id": "sale:CM:A", "movement_type": "sale", "days_since_event": 1},
        ]
    }
    result = send_deadline_day_movements(_session())
    mock_send.assert_not_called()
    assert result["ok"] is True
    assert result["results"][0]["due_count"] == 0


@patch("app.services.cts_submit.cts_ddts_is_configured", return_value=True)
@patch("app.services.cts_submit.cts_configured_farms", return_value=["CM"])
@patch("app.services.cts_submit.send_pending_movements")
@patch("app.services.cts_submit.list_pending_movements")
def test_send_deadline_day_dry_run_does_not_submit(
    mock_pending, mock_send, _ready, _ddts
) -> None:
    mock_pending.return_value = {
        "rows": [
            {"id": "sale:CM:A", "movement_type": "sale", "days_since_event": 3},
        ]
    }
    result = send_deadline_day_movements(_session(), dry_run=True)
    mock_send.assert_not_called()
    assert result["dry_run"] is True
    assert result["results"][0]["due_count"] == 1
    assert "Dry run" in result["results"][0]["message"]
