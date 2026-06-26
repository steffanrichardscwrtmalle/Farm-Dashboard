"""DocuSeal REST API client (httpx, no official Python SDK)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import DOCUSEAL_API_KEY, DOCUSEAL_BASE_URL, DOCUSEAL_WEBHOOK_SECRET

logger = logging.getLogger(__name__)


class DocuSealError(Exception):
    """DocuSeal API request failed."""


def _headers() -> dict[str, str]:
    if not DOCUSEAL_API_KEY:
        raise DocuSealError("DOCUSEAL_API_KEY is not configured.")
    return {
        "X-Auth-Token": DOCUSEAL_API_KEY,
        "Content-Type": "application/json",
    }


def _build_field_values(employee_data: dict[str, Any]) -> dict[str, str]:
    """Map employee fields to DocuSeal template field names."""
    pay_rate = employee_data.get("pay_rate") or ""
    pay_type = employee_data.get("pay_type") or ""
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
        "date_today": employee_data.get("date_today") or "",
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
    }


def create_submission(
    *,
    template_id: int,
    employee_data: dict[str, Any],
    reviewer_emails: list[str],
    staff_role: str = "Employee",
    reviewer_role: str = "HR Reviewer",
) -> int:
    """Create a sequential DocuSeal submission; returns submission id."""
    values = _build_field_values(employee_data)
    fields = [{"name": key, "default_value": val} for key, val in values.items() if val]

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

    payload = {
        "template_id": template_id,
        "send_email": True,
        "order": "preserved",
        "submitters": submitters,
    }

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{DOCUSEAL_BASE_URL}/submissions",
            headers=_headers(),
            json=payload,
        )
    if response.status_code >= 400:
        logger.error("DocuSeal create submission failed: %s", response.text[:500])
        raise DocuSealError(
            f"DocuSeal submission failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    data = response.json()
    submission_id = data.get("id")
    if submission_id is None and isinstance(data, list) and data:
        submission_id = data[0].get("submission_id") or data[0].get("id")
    if submission_id is None:
        raise DocuSealError("DocuSeal did not return a submission id.")
    return int(submission_id)


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
