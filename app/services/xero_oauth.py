"""Xero OAuth 2.0 helpers (authorization code + refresh)."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import (
    SECRET_KEY,
    XERO_AUTHORIZE_URL,
    XERO_CLIENT_ID,
    XERO_CLIENT_SECRET,
    XERO_CONNECTIONS_URL,
    XERO_REDIRECT_URI,
    XERO_SCOPES,
    XERO_TOKEN_URL,
)
from app.services.xero_auth import XeroAuthError

_STATE_SERIALIZER = URLSafeSerializer(SECRET_KEY, salt="xero-oauth-state")


def build_oauth_state(*, user_id: int, return_to: str = "/xero") -> str:
    return _STATE_SERIALIZER.dumps(
        {
            "uid": int(user_id),
            "return_to": return_to,
            "nonce": secrets.token_urlsafe(12),
        }
    )


def parse_oauth_state(state: str) -> dict[str, Any]:
    try:
        payload = _STATE_SERIALIZER.loads(state)
    except BadSignature as exc:
        raise XeroAuthError("Sign-in was interrupted. Please try again.") from exc
    if not isinstance(payload, dict) or "uid" not in payload:
        raise XeroAuthError("Sign-in was interrupted. Please try again.")
    return_to = str(payload.get("return_to") or "/xero")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/xero"
    return {"user_id": int(payload["uid"]), "return_to": return_to}


def build_authorize_url(*, state: str) -> str:
    if not XERO_CLIENT_ID or not XERO_CLIENT_SECRET:
        raise XeroAuthError(
            "Xero Client ID / Secret are not configured. Set XERO_CLIENT_ID and "
            "XERO_CLIENT_SECRET in the environment."
        )
    params = {
        "response_type": "code",
        "client_id": XERO_CLIENT_ID,
        "redirect_uri": XERO_REDIRECT_URI,
        "scope": XERO_SCOPES,
        "state": state,
    }
    return f"{XERO_AUTHORIZE_URL}?{urlencode(params)}"


def _token_request(client: httpx.Client, data: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        XERO_TOKEN_URL,
        data=data,
        auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise XeroAuthError(f"Xero token request failed. ({detail[:240]})")
    payload = response.json()
    if not payload.get("refresh_token") and data.get("grant_type") == "authorization_code":
        raise XeroAuthError(
            "Xero did not return a refresh token. Ensure offline_access is in the app scopes."
        )
    return payload


def exchange_authorization_code(client: httpx.Client, *, code: str) -> dict[str, Any]:
    return _token_request(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": XERO_REDIRECT_URI,
        },
    )


def refresh_access_token(client: httpx.Client, *, refresh_token: str) -> dict[str, Any]:
    return _token_request(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def fetch_connections(client: httpx.Client, *, access_token: str) -> list[dict[str, Any]]:
    response = client.get(
        XERO_CONNECTIONS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise XeroAuthError(f"Failed to load Xero organisations. ({detail[:240]})")
    payload = response.json()
    if not isinstance(payload, list):
        raise XeroAuthError("Unexpected Xero connections response.")
    return payload
