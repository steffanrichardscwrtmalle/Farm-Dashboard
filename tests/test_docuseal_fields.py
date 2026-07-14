"""Tests for DocuSeal field prefills."""

from __future__ import annotations

from app.services.docuseal_api import (
    _uk_date_to_iso,
    build_submitter_fields,
)


def test_uk_date_to_iso():
    assert _uk_date_to_iso("29/06/2026") == "2026-06-29"
    assert _uk_date_to_iso("2026-06-29") == "2026-06-29"
    assert _uk_date_to_iso("") == ""


def test_build_submitter_fields_marks_prefill_readonly():
    fields = build_submitter_fields(
        {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "dob": "01/02/2000",
            "start_date": "15/03/2026",
            "date_today": "14/07/2026",
        },
        {
            "full_name": {"types": {"text"}, "readonly": False},
            "email": {"types": {"text"}, "readonly": True},
            "dob": {"types": {"date"}, "readonly": False},
            "start_date": {"types": {"date"}, "readonly": False},
            "Date": {"types": {"date"}, "readonly": False},
        },
    )
    by_name = {f["name"]: f for f in fields}
    assert by_name["full_name"]["default_value"] == "Jane Doe"
    assert by_name["full_name"]["readonly"] is True
    assert by_name["email"]["readonly"] is True
    assert by_name["dob"]["default_value"] == "2000-02-01"
    assert by_name["start_date"]["default_value"] == "2026-03-15"
    assert by_name["Date"]["default_value"] == "2026-07-14"
    assert by_name["Date"]["readonly"] is True


def test_build_submitter_fields_skips_unknown_template_names():
    fields = build_submitter_fields(
        {"full_name": "Jane Doe", "mystery": "x"},
        {"full_name": {"types": {"text"}, "readonly": True}},
    )
    assert [f["name"] for f in fields] == ["full_name"]
