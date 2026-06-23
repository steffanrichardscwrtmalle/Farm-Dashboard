"""Feedlync OAuth reconnect and connection status."""

from __future__ import annotations

import secrets
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.models import User
from app.services.feedlync_auth import (
    FeedlyncAuthError,
    get_connection_status,
    save_refresh_token,
)
from app.services.feedlync_oauth import (
    build_authorize_url,
    exchange_authorization_code,
    generate_pkce_pair,
)

router = APIRouter(prefix="/api/feedlync")


class FeedlyncTokenBody(BaseModel):
    refresh_token: str = Field(min_length=20)


@router.get("/status")
def api_feedlync_status(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return get_connection_status(db)


@router.get("/oauth/start")
def api_feedlync_oauth_start(
    request: Request,
    return_to: str = "/feed-rate",
    _: User = Depends(get_current_user),
):
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/feed-rate"
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    request.session["feedlync_pkce_verifier"] = verifier
    request.session["feedlync_oauth_state"] = state
    request.session["feedlync_oauth_return"] = return_to
    url = build_authorize_url(code_challenge=challenge, state=state)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
def api_feedlync_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return_to = request.session.pop("feedlync_oauth_return", "/feed-rate")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/feed-rate"

    if error:
        detail = error_description or error
        return RedirectResponse(
            f"/feed-rate/connect?error={quote(detail)}&return={quote(return_to)}",
            status_code=302,
        )

    expected_state = request.session.pop("feedlync_oauth_state", None)
    verifier = request.session.pop("feedlync_pkce_verifier", None)
    if not code or not verifier or state != expected_state:
        return RedirectResponse(
            f"/feed-rate/connect?error={quote('Sign-in was interrupted. Please try again.')}"
            f"&return={quote(return_to)}",
            status_code=302,
        )

    try:
        with httpx.Client(timeout=60.0) as client:
            refresh_token = exchange_authorization_code(
                client, code=code, code_verifier=verifier
            )
        save_refresh_token(db, refresh_token, user_id=user.id)
    except FeedlyncAuthError as exc:
        return RedirectResponse(
            f"/feed-rate/connect?error={quote(str(exc))}&return={quote(return_to)}",
            status_code=302,
        )

    separator = "&" if "?" in return_to else "?"
    return RedirectResponse(
        f"{return_to}{separator}feedlync=connected&retry_import=1",
        status_code=302,
    )


@router.post("/connect")
def api_feedlync_connect_token(
    body: FeedlyncTokenBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        save_refresh_token(db, body.refresh_token, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "connected", "message": "FeedLync connected."}
