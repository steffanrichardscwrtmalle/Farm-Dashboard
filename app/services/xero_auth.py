"""Persist and resolve Xero OAuth tokens."""

from __future__ import annotations

import datetime as dt

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, XERO_CLIENT_ID, XERO_CLIENT_SECRET
from app.models import XeroAuth

_TOKEN_SERIALIZER = URLSafeSerializer(SECRET_KEY, salt="xero-oauth-token")
_SINGLETON_ID = 1


class XeroAuthError(Exception):
    """Xero auth is missing, invalid, or rejected."""


def _encrypt_token(token: str) -> str:
    return _TOKEN_SERIALIZER.dumps(token.strip())


def _decrypt_token(payload: str) -> str:
    try:
        value = _TOKEN_SERIALIZER.loads(payload)
    except BadSignature as exc:
        raise XeroAuthError("Stored Xero token is invalid.") from exc
    if not isinstance(value, str) or not value.strip():
        raise XeroAuthError("Stored Xero token is invalid.")
    return value.strip()


def credentials_configured() -> bool:
    return bool(XERO_CLIENT_ID and XERO_CLIENT_SECRET)


def get_auth_row(db: Session) -> XeroAuth | None:
    return db.get(XeroAuth, _SINGLETON_ID)


def get_stored_refresh_token(db: Session) -> str | None:
    row = get_auth_row(db)
    if row is None or not row.refresh_token:
        return None
    return _decrypt_token(row.refresh_token)


def get_stored_access_token(db: Session) -> tuple[str | None, dt.datetime | None]:
    row = get_auth_row(db)
    if row is None or not row.access_token:
        return None, None
    return _decrypt_token(row.access_token), row.access_token_expires_at


def save_tokens(
    db: Session,
    *,
    refresh_token: str,
    access_token: str | None = None,
    expires_in: int | None = None,
    user_id: int | None = None,
) -> None:
    refresh = refresh_token.strip()
    if not refresh:
        raise ValueError("Refresh token must not be empty.")

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    expires_at = None
    if access_token and expires_in:
        expires_at = now + dt.timedelta(seconds=max(int(expires_in) - 60, 0))

    row = get_auth_row(db)
    if row is None:
        row = XeroAuth(
            id=_SINGLETON_ID,
            refresh_token=_encrypt_token(refresh),
            access_token=_encrypt_token(access_token) if access_token else None,
            access_token_expires_at=expires_at,
            connected_at=now,
            updated_at=now,
            connected_by_user_id=user_id,
        )
        db.add(row)
    else:
        row.refresh_token = _encrypt_token(refresh)
        if access_token:
            row.access_token = _encrypt_token(access_token)
            row.access_token_expires_at = expires_at
        row.updated_at = now
        if user_id is not None:
            row.connected_by_user_id = user_id
    db.commit()


def clear_tokens(db: Session) -> None:
    row = get_auth_row(db)
    if row is None:
        return
    db.delete(row)
    db.commit()


def get_connection_status(db: Session) -> dict[str, object]:
    row = get_auth_row(db)
    return {
        "credentials_configured": credentials_configured(),
        "connected": row is not None and bool(row.refresh_token),
        "connected_at": row.connected_at.isoformat() if row and row.connected_at else None,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "has_access_token": bool(row and row.access_token),
        "access_token_expires_at": (
            row.access_token_expires_at.isoformat()
            if row and row.access_token_expires_at
            else None
        ),
    }
