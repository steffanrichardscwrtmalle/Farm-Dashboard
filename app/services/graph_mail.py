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


def _message_sender(message: dict) -> str:
    return (
        (message.get("from") or {})
        .get("emailAddress", {})
        .get("address", "")
        or ""
    ).lower()


def iter_attachments(
    mailbox: str,
    *,
    sender: str | None = None,
    sender_domain: str | None = None,
    extra_senders: tuple[str, ...] = (),
    since: dt.datetime,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...] = (),
    token: str | None = None,
) -> Iterator[dict]:
    """Yield attachments matching ``extensions``/``content_types`` from a mailbox.

    Filter senders with either ``sender`` (an exact address, filtered server-side)
    or ``sender_domain`` (a domain suffix such as 'example.co.uk', filtered in
    Python so we avoid Graph's $filter limitations on ``endswith``).
    ``extra_senders`` is a tuple of additional exact addresses (e.g. your own
    address when forwarding a missing statement) that are accepted alongside
    ``sender_domain``; matching is done in Python, case-insensitively. ``token``
    lets the caller supply an access token for a specific tenant/app (e.g. the
    separate Cwrt Malle app registration); when omitted, the default app's token
    is used.

    Each yielded dict has: message_id, subject, received (str), filename, content (bytes).
    """
    if token is None:
        token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    clauses = [
        "hasAttachments eq true",
        f"receivedDateTime ge {_iso_utc(since)}",
    ]
    select_fields = "id,subject,receivedDateTime"
    if sender:
        clauses.insert(0, f"from/emailAddress/address eq '{sender}'")
    if sender_domain or extra_senders:
        # Sender matching happens in Python, so we need the sender address back.
        select_fields += ",from"
    filter_expr = " and ".join(clauses)

    filter_safe = "()/',: "
    # Do not combine $orderby with $filter — Graph returns 400 InefficientFilter.
    query = (
        f"?$select={select_fields}"
        f"&$top={_PAGE_SIZE}"
        f"&$filter={quote(filter_expr, safe=filter_safe)}"
    )
    url = f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages{query}"

    ext = tuple(e.lower() for e in extensions)
    ctypes = tuple(c.lower() for c in content_types)
    domain_suffix = ("@" + sender_domain.lstrip("@").lower()) if sender_domain else None
    allowed_senders = frozenset(a.lstrip("@").lower() for a in extra_senders if a)

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
                if domain_suffix or allowed_senders:
                    addr = _message_sender(message)
                    domain_ok = bool(domain_suffix) and addr.endswith(domain_suffix)
                    exact_ok = addr in allowed_senders
                    if not (domain_ok or exact_ok):
                        continue
                yield from _iter_message_attachments(
                    client, headers, mailbox, message, ext, ctypes
                )
            url = payload.get("@odata.nextLink")


def iter_pdf_attachments(
    mailbox: str,
    *,
    sender: str,
    since: dt.datetime,
    token: str | None = None,
) -> Iterator[dict]:
    """Yield PDF attachments from a mailbox sent by ``sender`` since ``since``."""
    yield from iter_attachments(
        mailbox,
        sender=sender,
        since=since,
        extensions=(".pdf",),
        content_types=("application/pdf",),
        token=token,
    )


def _is_item_attachment(attachment: dict) -> bool:
    return "itemattachment" in (attachment.get("@odata.type") or "").lower()


def _emit_file_attachment(
    attachment: dict,
    *,
    message_id: str,
    subject: str,
    received: str,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...],
) -> dict | None:
    name = (attachment.get("name") or "").lower()
    content_type = (attachment.get("contentType") or "").lower()
    matches = name.endswith(extensions) or (
        bool(content_types) and content_type in content_types
    )
    content_b64 = attachment.get("contentBytes")
    if not matches or not content_b64:
        return None
    return {
        "message_id": message_id,
        "subject": subject,
        "received": received,
        "filename": attachment.get("name") or "",
        "content": base64.b64decode(content_b64),
    }


def _walk_embedded_attachments(
    item: dict,
    *,
    message_id: str,
    subject: str,
    received: str,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...],
) -> Iterator[dict]:
    """Walk attachments of an embedded Outlook item (and any deeper items).

    A single ``$expand=microsoft.graph.itemattachment/item`` returns nested
    attachments up to 30 levels, so embedded item attachments already carry
    their own expanded ``item``; recurse into those without extra requests.
    """
    for attachment in item.get("attachments") or []:
        if _is_item_attachment(attachment):
            nested = attachment.get("item") or {}
            if nested:
                yield from _walk_embedded_attachments(
                    nested,
                    message_id=message_id,
                    subject=subject,
                    received=received,
                    extensions=extensions,
                    content_types=content_types,
                )
            continue
        emitted = _emit_file_attachment(
            attachment,
            message_id=message_id,
            subject=subject,
            received=received,
            extensions=extensions,
            content_types=content_types,
        )
        if emitted:
            yield emitted


def _iter_item_attachment(
    client: httpx.Client,
    headers: dict,
    mailbox: str,
    message_id: str,
    attachment_id: str,
    subject: str,
    received: str,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...],
) -> Iterator[dict]:
    """Expand an Outlook item attachment and yield matching nested files."""
    url = (
        f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages/{message_id}"
        f"/attachments/{attachment_id}"
        "?$expand=microsoft.graph.itemattachment/item"
    )
    response = client.get(url, headers=headers)
    response.raise_for_status()
    item = response.json().get("item") or {}
    yield from _walk_embedded_attachments(
        item,
        message_id=message_id,
        subject=subject,
        received=received,
        extensions=extensions,
        content_types=content_types,
    )


def _iter_message_attachments(
    client: httpx.Client,
    headers: dict,
    mailbox: str,
    message: dict,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...],
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
    attachments = response.json().get("value", [])
    for attachment in attachments:
        if _is_item_attachment(attachment):
            attachment_id = attachment.get("id")
            if attachment_id:
                yield from _iter_item_attachment(
                    client,
                    headers,
                    mailbox,
                    message_id,
                    attachment_id,
                    subject,
                    received,
                    extensions,
                    content_types,
                )
            continue
        emitted = _emit_file_attachment(
            attachment,
            message_id=message_id,
            subject=subject,
            received=received,
            extensions=extensions,
            content_types=content_types,
        )
        if emitted:
            yield emitted
