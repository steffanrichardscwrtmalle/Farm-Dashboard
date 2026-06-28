"""Send email via Microsoft Graph (application permissions)."""

from __future__ import annotations

import base64

import httpx

from app.config import GRAPH_DRIVE_USER_EMAIL
from app.services.graph_onedrive import GraphConfigError, get_access_token


class GraphEmailError(Exception):
    """Outbound email could not be sent."""


def _text_to_html(text: str) -> str:
    """Escape and convert a plain-text body into minimal HTML."""
    escaped = (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return "<html><body>" + escaped.replace("\n", "<br>") + "</body></html>"


def send_mail_with_attachment(
    *,
    to: str,
    subject: str,
    body: str,
    filename: str,
    content_bytes: bytes,
    content_type: str = "text/csv",
) -> None:
    """Send an email with a file attachment from GRAPH_DRIVE_USER_EMAIL.

    The body is sent as HTML so Exchange does not fall back to the TNEF
    (winmail.dat) encoding, which can hide the attachment from non-Outlook
    or external mail clients.
    """
    recipient = (to or "").strip()
    if not recipient:
        raise GraphEmailError("Recipient email is required.")
    if not GRAPH_DRIVE_USER_EMAIL:
        raise GraphEmailError("GRAPH_DRIVE_USER_EMAIL is not configured.")

    try:
        token = get_access_token()
    except GraphConfigError as exc:
        raise GraphEmailError(str(exc)) from exc

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": _text_to_html(body),
            },
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": content_type,
                    "contentBytes": base64.b64encode(content_bytes).decode("ascii"),
                }
            ],
        },
        "saveToSentItems": True,
    }

    url = f"https://graph.microsoft.com/v1.0/users/{GRAPH_DRIVE_USER_EMAIL}/sendMail"
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise GraphEmailError(
                f"Graph sendMail failed ({response.status_code}): {detail}"
            )
