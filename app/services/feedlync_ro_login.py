"""Unattended FeedLync login by replicating the Azure AD B2C sign-in flow.

FeedLync's B2C tenant only exposes an interactive (browser) sign-in policy:
ROPC is not enabled and our own OAuth redirect URI is not registered. Since the
account has no MFA, we can reproduce the browser's HTTP exchanges directly:

  1. GET the authorize endpoint -> HTML page embeds a SETTINGS object with a
     CSRF token and transaction id, and sets x-ms-cpim-* cookies.
  2. POST credentials to the SelfAsserted endpoint.
  3. GET the confirmed endpoint -> 302 redirect carrying the authorization code.
  4. Exchange the code (with our PKCE verifier) for a refresh token.

This depends on B2C's current sign-in flow; if FeedLync changes it, the import
falls back to the existing "reconnect" error and a token can be pasted manually.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import (
    FEEDLYNC_AUTHORIZE_URL,
    FEEDLYNC_CLIENT_ID,
    FEEDLYNC_PASSWORD,
    FEEDLYNC_SPA_REDIRECT_URI,
    FEEDLYNC_TOKEN_SCOPE,
    FEEDLYNC_TOKEN_URL,
    FEEDLYNC_USERNAME,
)
from app.services.feedlync_auth import FeedlyncAuthError
from app.services.feedlync_oauth import generate_pkce_pair

_SETTINGS_START_RE = re.compile(r"var SETTINGS\s*=\s*\{")


def _settings_field(html: str, field: str) -> str | None:
    # SETTINGS is not strict JSON (it embeds JS), so pull individual string
    # fields rather than parsing the whole object.
    match = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    return match.group(1) if match else None


def auto_login_enabled() -> bool:
    return bool(FEEDLYNC_USERNAME and FEEDLYNC_PASSWORD)


def _policy_from_url(url: str) -> str:
    # .../<tenant>.onmicrosoft.com/<policy>/oauth2/v2.0/authorize
    parts = urlparse(url).path.strip("/").split("/")
    for part in parts:
        if part.lower().startswith("b2c_1"):
            return part
    raise FeedlyncAuthError("Could not determine FeedLync B2C policy name.")


def _base_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    # host + /<tenant>/<policy>
    return f"{parsed.scheme}://{parsed.netloc}/{parts[0]}/{parts[1]}"


def _extract_settings(html: str) -> dict:
    if not _SETTINGS_START_RE.search(html):
        raise FeedlyncAuthError(
            "FeedLync sign-in page format changed (no SETTINGS). "
            "Reconnect FeedLync manually."
        )
    return {
        "csrf": _settings_field(html, "csrf"),
        "transId": _settings_field(html, "transId"),
        "api": _settings_field(html, "api"),
    }


def fetch_refresh_token_via_login() -> str:
    """Log in with stored credentials and return a fresh refresh token."""
    if not auto_login_enabled():
        raise FeedlyncAuthError(
            "FeedLync auto-login is not configured "
            "(set FEEDLYNC_USERNAME and FEEDLYNC_PASSWORD)."
        )

    verifier, challenge = generate_pkce_pair()
    policy = _policy_from_url(FEEDLYNC_AUTHORIZE_URL)
    base = _base_from_url(FEEDLYNC_AUTHORIZE_URL)

    authorize_params = {
        "client_id": FEEDLYNC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": FEEDLYNC_SPA_REDIRECT_URI,
        "scope": FEEDLYNC_TOKEN_SCOPE,
        "response_mode": "query",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        # Step 1: load the sign-in page to obtain CSRF + transaction id + cookies.
        page = client.get(FEEDLYNC_AUTHORIZE_URL, params=authorize_params)
        if page.status_code >= 400:
            raise FeedlyncAuthError(
                f"FeedLync authorize request failed ({page.status_code})."
            )
        settings = _extract_settings(page.text)
        csrf = settings.get("csrf")
        trans_id = settings.get("transId")
        api = settings.get("api") or "CombinedSigninAndSignup"
        if not csrf or not trans_id:
            raise FeedlyncAuthError("FeedLync sign-in tokens missing from page.")

        # Step 2: submit credentials.
        self_asserted = client.post(
            f"{base}/SelfAsserted",
            params={"tx": trans_id, "p": policy},
            data={
                "request_type": "RESPONSE",
                "email": FEEDLYNC_USERNAME,
                "password": FEEDLYNC_PASSWORD,
            },
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if self_asserted.status_code >= 400:
            raise FeedlyncAuthError(
                f"FeedLync credential submission failed ({self_asserted.status_code})."
            )
        try:
            sa_payload = self_asserted.json()
        except ValueError:
            sa_payload = {}
        if str(sa_payload.get("status")) not in ("200", "None", ""):
            message = sa_payload.get("message") or "FeedLync rejected the credentials."
            raise FeedlyncAuthError(message)

        # Step 3: confirm to receive the authorization code via redirect.
        confirmed = client.get(
            f"{base}/api/{api}/confirmed",
            params={
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": trans_id,
                "p": policy,
            },
        )
        location = confirmed.headers.get("location", "")
        code = _extract_code(location)
        if not code:
            raise FeedlyncAuthError(
                "FeedLync sign-in did not return an authorization code "
                f"(status {confirmed.status_code})."
            )

        # Step 4: exchange the code for a refresh token.
        token_resp = client.post(
            FEEDLYNC_TOKEN_URL,
            data={
                "client_id": FEEDLYNC_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": FEEDLYNC_SPA_REDIRECT_URI,
                "code_verifier": verifier,
                "scope": FEEDLYNC_TOKEN_SCOPE,
                "client_info": "1",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code >= 400:
            raise FeedlyncAuthError(
                f"FeedLync token exchange failed ({token_resp.status_code}): "
                f"{token_resp.text[:200]}"
            )
        refresh_token = token_resp.json().get("refresh_token")
        if not refresh_token:
            raise FeedlyncAuthError("FeedLync login did not return a refresh token.")
        return str(refresh_token)


def _extract_code(location: str) -> str | None:
    if not location:
        return None
    parsed = urlparse(location)
    # response_mode=query -> code in query string
    if parsed.query:
        values = parse_qs(parsed.query).get("code")
        if values:
            return values[0]
    # fallback: fragment (response_mode=fragment)
    if parsed.fragment:
        values = parse_qs(parsed.fragment).get("code")
        if values:
            return values[0]
    return None
