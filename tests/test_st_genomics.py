"""ST Strategy female export decoding and large OneDrive uploads."""

from __future__ import annotations

import base64

import pytest

from app.services import graph_onedrive
from app.services.st_genomics import (
    StGenomicsError,
    safe_workbook_name,
    workbook_from_export_payload,
)


def test_workbook_from_export_payload_decodes_xlsx() -> None:
    content = b"PK\x03\x04workbook"
    payload = {
        "success": True,
        "errors": [],
        "resultData": {
            "fileName": "Females All List GBR - 2026-11-18.xlsx",
            "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content": base64.b64encode(content).decode("ascii"),
        },
    }
    name, decoded = workbook_from_export_payload(payload)
    assert name == "Females All List GBR - 2026-11-18.xlsx"
    assert decoded == content


def test_safe_workbook_name_strips_path_and_illegal_characters() -> None:
    assert safe_workbook_name(r"..\Females: All.xlsx") == "Females- All.xlsx"


def test_workbook_payload_rejects_a_failed_export() -> None:
    with pytest.raises(StGenomicsError):
        workbook_from_export_payload({"success": False, "resultData": {}})


def test_large_onedrive_upload_uses_a_session(monkeypatch) -> None:
    monkeypatch.setattr(graph_onedrive, "LOCAL_HERD_EXPORT_DIR", "")
    monkeypatch.setattr(graph_onedrive, "GRAPH_TENANT_ID", "tenant")
    monkeypatch.setattr(graph_onedrive, "GRAPH_CLIENT_ID", "client")
    monkeypatch.setattr(graph_onedrive, "GRAPH_CLIENT_SECRET", "secret")
    monkeypatch.setattr(graph_onedrive, "GRAPH_DRIVE_USER_EMAIL", "herd@farm.test")
    monkeypatch.setattr(graph_onedrive, "HERD_EXPORT_BASE_PATH", "Herd")
    monkeypatch.setattr(graph_onedrive, "_SIMPLE_UPLOAD_MAX_BYTES", 8)
    monkeypatch.setattr(graph_onedrive, "get_access_token", lambda: "token")

    calls: list[tuple] = []

    class _Response:
        def __init__(self, payload: dict | None = None):
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, headers=None, json=None):
            calls.append(("post", url, json))
            return _Response({"uploadUrl": "https://upload.example/session"})

        def put(self, url, headers=None, content=None):
            calls.append(("put", url, headers.get("Content-Range"), content))
            return _Response()

    monkeypatch.setattr(graph_onedrive.httpx, "Client", lambda **kwargs: _Client())

    graph_onedrive.upload_herd_file(
        "Genomic Results/Females.xlsx",
        b"123456789",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert calls[0][0] == "post"
    assert calls[0][1].endswith(":/createUploadSession")
    assert calls[0][2]["item"]["@microsoft.graph.conflictBehavior"] == "replace"
    assert calls[1][0] == "put"
    assert calls[1][1] == "https://upload.example/session"
    assert calls[1][2] == "bytes 0-8/9"
    assert calls[1][3] == b"123456789"
    assert not any(isinstance(call[1], str) and "Authorization" in call[1] for call in calls)
