"""Read inbound email + attachments via Microsoft Graph (application permissions).

Requires the Mail.Read application permission on the app registration (ideally
scoped to the relevant mailboxes via an Exchange application access policy).
"""

from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Iterator
from urllib.parse import quote

import httpx

from app.services.graph_onedrive import get_access_token

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_PAGE_SIZE = 50


def _iso_utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_pdf_attachments(
    mailbox: str,
    *,
    sender: str,
    since: dt.datetime,
    token: str | None = None,
) -> Iterator[dict]:
    """Yield PDF attachments from a mailbox sent by ``sender`` since ``since``.

    ``token`` lets the caller supply an access token for a specific tenant/app
    (e.g. the separate Cwrt Malle app registration). When omitted, the default
    app's token is used.

    Each yielded dict has: message_id, subject, received (str), filename, content (bytes).
    """
    if token is None:
        token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    filter_expr = (
        f"from/emailAddress/address eq '{sender}' "
        "and hasAttachments eq true "
        f"and receivedDateTime ge {_iso_utc(since)}"
    )
    filter_safe = "()/',: "
    # Do not combine $orderby with $filter — Graph returns 400 InefficientFilter.
    query = (
        "?$select=id,subject,receivedDateTime"
        f"&$top={_PAGE_SIZE}"
        f"&$filter={quote(filter_expr, safe=filter_safe)}"
    )
    url = f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages{query}"

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        while url:
            response = client.get(url, headers=headers)
            if response.status_code == 404:
                raise FileNotFoundError(f"Mailbox not found: {mailbox}")
            response.raise_for_status()
            payload = response.json()
            messages = payload.get("value", [])
            messages.sort(
                key=lambda item: item.get("receivedDateTime") or "",
                reverse=True,
            )
            for message in messages:
                yield from _iter_message_pdfs(client, headers, mailbox, message)
            url = payload.get("@odata.nextLink")


def _iter_message_pdfs(
    client: httpx.Client,
    headers: dict,
    mailbox: str,
    message: dict,
) -> Iterator[dict]:
    message_id = message.get("id")
    if not message_id:
        return
    subject = message.get("subject") or ""
    received = message.get("receivedDateTime") or ""

    att_url = (
        f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages/{message_id}/attachments"
    )
    response = client.get(att_url, headers=headers)
    response.raise_for_status()
    for attachment in response.json().get("value", []):
        name = attachment.get("name") or ""
        content_type = (attachment.get("contentType") or "").lower()
        is_pdf = name.lower().endswith(".pdf") or content_type == "application/pdf"
        content_b64 = attachment.get("contentBytes")
        if not is_pdf or not content_b64:
            continue
        yield {
            "message_id": message_id,
            "subject": subject,
            "received": received,
            "filename": name,
            "content": base64.b64decode(content_b64),
        }
