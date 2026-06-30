"""Read inbound email + attachments via Microsoft Graph (application permissions).

Requires the Mail.Read application permission on the app registration (ideally
scoped to the relevant mailboxes via an Exchange application access policy).
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from collections.abc import Callable, Iterator
from urllib.parse import quote

import httpx

from app.services.graph_onedrive import get_access_token

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_PAGE_SIZE = 50
logger = logging.getLogger(__name__)


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
    skip_message_ids: frozenset[str] = frozenset(),
    since: dt.datetime,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...] = (),
    token: str | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> Iterator[dict]:
    """Yield attachments matching ``extensions``/``content_types`` from a mailbox.

    Filter senders with either ``sender`` (an exact address, filtered server-side)
    or ``sender_domain`` (a domain suffix such as 'example.co.uk', filtered in
    Python so we avoid Graph's $filter limitations on ``endswith``). ``token`` lets
    the caller supply an access token for a specific tenant/app (e.g. the separate
    Cwrt Malle app registration); when omitted, the default app's token is used.

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
    if sender_domain:
        # Domain matching happens in Python, so we need the sender address back.
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
    messages_checked = 0
    pdfs_found = 0

    def report(phase: str) -> None:
        if on_progress:
            on_progress(phase, messages_checked, pdfs_found)

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
                if message.get("id") in skip_message_ids:
                    continue
                if domain_suffix:
                    addr = _message_sender(message)
                    if not addr.endswith(domain_suffix):
                        continue
                messages_checked += 1
                if messages_checked % 5 == 0:
                    report("Scanning mailbox")
                for attachment in _iter_message_attachments(
                    client, headers, mailbox, message, ext, ctypes
                ):
                    pdfs_found += 1
                    yield attachment
            url = payload.get("@odata.nextLink")


def iter_statement_attachments(
    mailbox: str,
    *,
    sender_domains: tuple[str, ...] = (),
    skip_message_ids: frozenset[str] = frozenset(),
    since: dt.datetime,
    extensions: tuple[str, ...],
    content_types: tuple[str, ...] = (),
    token: str | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> Iterator[dict]:
    """Yield statement PDFs from buyer-domain mail plus forwarded statement emails.

    Pass 1 filters by sender domain in Python (same approach as haulier import).
    Pass 2 picks up ``Milk Statement`` subjects when the sender is not the buyer
    (e.g. internal forwards) so monthly statements are not missed.
    """
    if token is None:
        token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    seen_message_ids: set[str] = set()
    messages_checked = 0
    pdfs_found = 0

    def report(phase: str) -> None:
        if on_progress:
            on_progress(phase, messages_checked, pdfs_found)

    def skip_ids() -> frozenset[str]:
        return skip_message_ids | frozenset(seen_message_ids)

    def track(attachment: dict) -> dict:
        nonlocal pdfs_found
        message_id = attachment.get("message_id")
        if message_id:
            seen_message_ids.add(message_id)
        pdfs_found += 1
        return attachment

    def domain_progress(phase: str, _messages: int, _pdfs: int) -> None:
        nonlocal messages_checked
        messages_checked = _messages
        pdfs_found = _pdfs
        report(phase)

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for domain in sender_domains:
            domain = domain.lstrip("@").lower()
            if not domain:
                continue
            report(f"Searching {domain}")
            for attachment in iter_attachments(
                mailbox,
                sender_domain=domain,
                skip_message_ids=skip_ids(),
                since=since,
                extensions=extensions,
                content_types=content_types,
                token=token,
                on_progress=domain_progress,
            ):
                yield track(attachment)

        report("Searching forwarded statements")
        clauses = [
            "contains(subject,'Milk Statement')",
            "hasAttachments eq true",
            f"receivedDateTime ge {_iso_utc(since)}",
        ]
        filter_expr = " and ".join(clauses)
        filter_safe = "()/',: "
        url = (
            f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages"
            f"?$filter={quote(filter_expr, safe=filter_safe)}"
            f"&$select=id,subject,receivedDateTime,from,hasAttachments"
            f"&$top={_PAGE_SIZE}"
            f"&$count=true"
        )
        advanced_headers = {**headers, "ConsistencyLevel": "eventual"}
        ext = tuple(e.lower() for e in extensions)
        ctypes = tuple(c.lower() for c in content_types)
        try:
            while url:
                response = client.get(url, headers=advanced_headers)
                if response.status_code == 404:
                    raise FileNotFoundError(f"Mailbox not found: {mailbox}")
                if response.status_code == 400:
                    logger.warning(
                        "Graph subject filter unavailable for %s; domain pass only",
                        mailbox,
                    )
                    break
                response.raise_for_status()
                payload = response.json()
                for message in payload.get("value", []):
                    message_id = message.get("id")
                    if not message_id or message_id in skip_ids():
                        continue
                    seen_message_ids.add(message_id)
                    messages_checked += 1
                    if messages_checked % 5 == 0:
                        report("Scanning mailbox")
                    for attachment in _iter_message_attachments(
                        client, headers, mailbox, message, ext, ctypes
                    ):
                        yield track(attachment)
                url = payload.get("@odata.nextLink")
        except httpx.HTTPStatusError:
            logger.warning(
                "Forwarded statement search failed for %s; domain pass only",
                mailbox,
            )


def iter_nml_pdf_attachments(
    mailbox: str,
    *,
    sender_domain: str,
    since: dt.datetime,
    token: str | None = None,
) -> Iterator[dict]:
    """Yield NML report PDFs from a mailbox.

  Pass 1 matches ``@sender_domain`` in Python (reliable across address casing).
  Pass 2 matches ``National Milk`` in the subject for forwarded reports.
    """
    if token is None:
        token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    seen_message_ids: set[str] = set()
    ext = (".pdf",)
    ctypes = ("application/pdf",)

    def skip_ids() -> frozenset[str]:
        return frozenset(seen_message_ids)

    def track(attachment: dict) -> dict:
        message_id = attachment.get("message_id")
        if message_id:
            seen_message_ids.add(message_id)
        return attachment

    for attachment in iter_attachments(
        mailbox,
        sender_domain=sender_domain,
        skip_message_ids=skip_ids(),
        since=since,
        extensions=ext,
        content_types=ctypes,
        token=token,
    ):
        yield track(attachment)

    clauses = [
        "contains(subject,'National Milk')",
        "hasAttachments eq true",
        f"receivedDateTime ge {_iso_utc(since)}",
    ]
    filter_expr = " and ".join(clauses)
    filter_safe = "()/',: "
    url = (
        f"{_GRAPH_BASE}/users/{quote(mailbox)}/messages"
        f"?$filter={quote(filter_expr, safe=filter_safe)}"
        f"&$select=id,subject,receivedDateTime,from,hasAttachments"
        f"&$top={_PAGE_SIZE}"
        f"&$count=true"
    )
    advanced_headers = {**headers, "ConsistencyLevel": "eventual"}
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        try:
            while url:
                response = client.get(url, headers=advanced_headers)
                if response.status_code == 404:
                    raise FileNotFoundError(f"Mailbox not found: {mailbox}")
                if response.status_code == 400:
                    logger.warning(
                        "Graph NML subject filter unavailable for %s; domain pass only",
                        mailbox,
                    )
                    break
                response.raise_for_status()
                payload = response.json()
                for message in payload.get("value", []):
                    message_id = message.get("id")
                    if not message_id or message_id in skip_ids():
                        continue
                    seen_message_ids.add(message_id)
                    for attachment in _iter_message_attachments(
                        client, headers, mailbox, message, ext, ctypes
                    ):
                        yield track(attachment)
                url = payload.get("@odata.nextLink")
        except httpx.HTTPStatusError:
            logger.warning(
                "NML subject search failed for %s; domain pass only",
                mailbox,
            )


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
