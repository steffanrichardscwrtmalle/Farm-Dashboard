"""DocuSeal REST API client (httpx, no official Python SDK)."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import httpx

from app.config import (
    DOCUSEAL_API_KEY,
    DOCUSEAL_BASE_URL,
    DOCUSEAL_WEBHOOK_SECRET,
    docuseal_email_for,
)

logger = logging.getLogger(__name__)


class DocuSealError(Exception):
    """DocuSeal API request failed."""


def _headers() -> dict[str, str]:
    if not DOCUSEAL_API_KEY:
        raise DocuSealError(
            "DOCUSEAL_API_KEY is not configured. "
            "Locally: set it in .env and fully restart the server. "
            "On Render: set DOCUSEAL_API_KEY in the web service Environment tab."
        )
    return {
        "X-Auth-Token": DOCUSEAL_API_KEY,
        "Content-Type": "application/json",
    }


def _build_field_values(employee_data: dict[str, Any]) -> dict[str, str]:
    """Map employee fields to DocuSeal template field names."""
    pay_rate = employee_data.get("pay_rate") or ""
    pay_type = employee_data.get("pay_type") or ""
    date_today = employee_data.get("date_today") or ""
    return {
        "business": employee_data.get("business") or "",
        "title": employee_data.get("title") or "",
        "full_name": employee_data.get("full_name") or "",
        "email": employee_data.get("email") or "",
        "phone": employee_data.get("phone") or "",
        "dob": employee_data.get("dob") or "",
        "address": employee_data.get("address") or "",
        "ni_number": employee_data.get("ni_number") or "",
        "hourly_rate": pay_rate if pay_type == "hourly" else "",
        "salary": pay_rate if pay_type == "salary" else "",
        "pay_rate": pay_rate,
        "pay_type": pay_type,
        "start_date": employee_data.get("start_date") or "",
        "role_title": employee_data.get("role_title") or "",
        "working_days_per_week": employee_data.get("working_days_per_week") or "",
        "working_hours_per_day": employee_data.get("working_hours_per_day") or "",
        "weekly_hours": employee_data.get("weekly_hours") or "",
        "date_today": date_today,
        # Some templates use DocuSeal's default "Date" label.
        "Date": date_today,
        "driving_license_number": employee_data.get("driving_license_number") or "",
        "license_points": employee_data.get("license_points") or "",
        "right_to_work_share_code": employee_data.get("right_to_work_share_code") or "",
        "bank_name": employee_data.get("bank_name") or "",
        "account_holder_name": employee_data.get("account_holder_name") or "",
        "sort_code": employee_data.get("sort_code") or "",
        "account_number": employee_data.get("account_number") or "",
        "next_of_kin_name": employee_data.get("next_of_kin_name") or "",
        "next_of_kin_relationship": employee_data.get("next_of_kin_relationship") or "",
        "next_of_kin_phone": employee_data.get("next_of_kin_phone") or "",
        "employer_name": "Steffan Richards",
        "employer_position": "Director",
    }


def _uk_date_to_iso(value: str) -> str:
    """Convert DD/MM/YYYY (or DD/MM/YY) to YYYY-MM-DD for DocuSeal date fields."""
    raw = (value or "").strip()
    if not raw:
        return ""
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def get_template_fields(template_id: int) -> dict[str, dict[str, Any]]:
    """Return template fields keyed by name: ``{name: {types, readonly}}``.

    Used to avoid 422 'Unknown field' errors and to format/lock values correctly.
    Returns an empty dict if the template can't be fetched.
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{DOCUSEAL_BASE_URL}/templates/{template_id}",
                headers=_headers(),
            )
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch template %s fields: %s", template_id, exc)
        return {}
    if response.status_code >= 400:
        logger.warning(
            "Could not fetch template %s fields (%s): %s",
            template_id,
            response.status_code,
            response.text[:200],
        )
        return {}
    data = response.json()
    fields: dict[str, dict[str, Any]] = {}
    for field in data.get("fields") or []:
        name = field.get("name")
        if not name:
            continue
        entry = fields.setdefault(name, {"types": set(), "readonly": False})
        field_type = field.get("type")
        if field_type:
            entry["types"].add(str(field_type).lower())
        if field.get("readonly") is True:
            entry["readonly"] = True
    return fields


def get_template_field_names(template_id: int) -> set[str]:
    """Return the set of field names defined on a DocuSeal template."""
    return set(get_template_fields(template_id))


def build_submitter_fields(
    employee_data: dict[str, Any],
    template_fields: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build DocuSeal submitter field prefills.

    Prefills are always sent as ``readonly: true`` so the employee cannot edit
    values collected on the enroll form — even if the DocuSeal template itself
    left those fields editable (as on the Green Acre copy).
    """
    values = _build_field_values(employee_data)
    known = set(template_fields or {})
    fields: list[dict[str, Any]] = []
    for key, val in values.items():
        if not val:
            continue
        if known and key not in known:
            continue
        out_val: str = val
        meta = (template_fields or {}).get(key) or {}
        types = meta.get("types") or set()
        if "date" in types:
            out_val = _uk_date_to_iso(val)
        fields.append(
            {
                "name": key,
                "default_value": out_val,
                "readonly": True,
            }
        )
    return fields


def _unknown_field_from_response(response: httpx.Response) -> str | None:
    """Extract the field name from a DocuSeal 'Unknown field: X' 422 error."""
    try:
        data = response.json()
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    if not error:
        return None
    match = re.search(r"Unknown field:\s*(\S+)", str(error))
    return match.group(1) if match else None


def create_submission(
    *,
    template_id: int,
    employee_data: dict[str, Any],
    reviewer_emails: list[str],
    staff_role: str = "Employee",
    reviewer_role: str = "HR Reviewer",
    business: str | None = None,
) -> int:
    """Create a sequential DocuSeal submission; returns submission id."""
    template_fields = get_template_fields(template_id)
    fields = build_submitter_fields(employee_data, template_fields or None)

    submitters: list[dict[str, Any]] = [
        {
            "email": employee_data["email"],
            "name": employee_data["full_name"],
            "role": staff_role,
            "send_email": True,
            "fields": fields,
            "external_id": f"employee_{employee_data.get('employee_id', 'new')}",
        }
    ]
    for email in reviewer_emails:
        submitters.append(
            {
                "email": email.strip(),
                "role": reviewer_role,
                "send_email": True,
            }
        )

    payload: dict[str, Any] = {
        "template_id": template_id,
        "send_email": True,
        "order": "preserved",
        "submitters": submitters,
    }

    subject, body = docuseal_email_for(business)
    message: dict[str, str] = {}
    if subject:
        message["subject"] = subject
    if body:
        message["body"] = body
    if message:
        payload["message"] = message

    with httpx.Client(timeout=60.0) as client:
        # Self-heal: if DocuSeal rejects a field the template doesn't define
        # (e.g. the template-field pre-fetch failed and we fell back to sending
        # everything), strip that field and retry rather than hard-failing.
        max_attempts = len(fields) + 1
        for _ in range(max_attempts):
            response = client.post(
                f"{DOCUSEAL_BASE_URL}/submissions",
                headers=_headers(),
                json=payload,
            )
            if response.status_code < 400:
                break
            unknown = _unknown_field_from_response(response)
            if not unknown:
                break
            remaining = [f for f in fields if f.get("name") != unknown]
            if len(remaining) == len(fields):
                break
            logger.warning(
                "DocuSeal rejected unknown field '%s'; retrying without it.",
                unknown,
            )
            fields[:] = remaining

    if response.status_code >= 400:
        logger.error("DocuSeal create submission failed: %s", response.text[:500])
        raise DocuSealError(
            f"DocuSeal submission failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json()
    submission_id = _extract_submission_id(data)
    if submission_id is None:
        raise DocuSealError("DocuSeal did not return a submission id.")
    return submission_id


def _extract_submission_id(data: Any) -> int | None:
    """DocuSeal may return a submission object or a list of submitter objects."""
    if isinstance(data, dict):
        raw = data.get("id") or data.get("submission_id")
        if raw is not None:
            return int(raw)
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            raw = item.get("submission_id") or item.get("id")
            if raw is not None:
                return int(raw)
    return None


def download_signed_pdf(submission_id: int) -> bytes:
    """Download merged signed PDF bytes for a completed submission."""
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        response = client.get(
            f"{DOCUSEAL_BASE_URL}/submissions/{submission_id}/documents",
            headers=_headers(),
            params={"merge": "true"},
        )
        if response.status_code >= 400:
            raise DocuSealError(
                f"DocuSeal documents fetch failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        payload = response.json()
        documents = payload.get("documents") or []
        if not documents:
            raise DocuSealError("No signed documents returned from DocuSeal.")
        doc_url = documents[0].get("url")
        if not doc_url:
            raise DocuSealError("DocuSeal document has no download URL.")
        pdf_response = client.get(doc_url)
        pdf_response.raise_for_status()
        return pdf_response.content


def verify_webhook_secret(*, header_secret: str | None, query_secret: str | None) -> bool:
    if not DOCUSEAL_WEBHOOK_SECRET:
        logger.warning("DOCUSEAL_WEBHOOK_SECRET not set; rejecting webhook.")
        return False
    provided = (header_secret or query_secret or "").strip()
    if not provided:
        return False
    return provided == DOCUSEAL_WEBHOOK_SECRET
