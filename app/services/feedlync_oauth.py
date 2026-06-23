"""Feedlync OAuth (PKCE) helpers for browser-based reconnect."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

from app.config import (
    FEEDLYNC_AUTHORIZE_URL,
    FEEDLYNC_CLIENT_ID,
    FEEDLYNC_REDIRECT_URI,
    FEEDLYNC_TOKEN_SCOPE,
    FEEDLYNC_TOKEN_URL,
)
from app.services.feedlync_auth import FeedlyncAuthError


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(*, code_challenge: str, state: str) -> str:
    params = {
        "client_id": FEEDLYNC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": FEEDLYNC_REDIRECT_URI,
        "scope": FEEDLYNC_TOKEN_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "response_mode": "query",
        "state": state,
    }
    return f"{FEEDLYNC_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_authorization_code(
    client: httpx.Client,
    *,
    code: str,
    code_verifier: str,
) -> str:
    response = client.post(
        FEEDLYNC_TOKEN_URL,
        data={
            "client_id": FEEDLYNC_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": FEEDLYNC_REDIRECT_URI,
            "code_verifier": code_verifier,
            "scope": FEEDLYNC_TOKEN_SCOPE,
            "client_info": "1",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise FeedlyncAuthError(
            "FeedLync sign-in failed. Use the manual token option below, or try again. "
            f"({detail[:200]})"
        )
    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise FeedlyncAuthError(
            "FeedLync sign-in did not return a refresh token. Use the manual token option."
        )
    return str(refresh_token)
