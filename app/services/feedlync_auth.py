"""Persist and resolve Feedlync refresh tokens."""

from __future__ import annotations

import datetime as dt

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import FEEDLYNC_REFRESH_TOKEN, SECRET_KEY
from app.models import FeedlyncAuth

_TOKEN_SERIALIZER = URLSafeSerializer(SECRET_KEY, salt="feedlync-refresh-token")
_SINGLETON_ID = 1


class FeedlyncAuthError(Exception):
    """Feedlync refresh token is missing or rejected."""


def _encrypt_token(token: str) -> str:
    return _TOKEN_SERIALIZER.dumps(token.strip())


def _decrypt_token(payload: str) -> str:
    try:
        value = _TOKEN_SERIALIZER.loads(payload)
    except BadSignature as exc:
        raise FeedlyncAuthError("Stored Feedlync token is invalid.") from exc
    if not isinstance(value, str) or not value.strip():
        raise FeedlyncAuthError("Stored Feedlync token is invalid.")
    return value.strip()


def get_stored_refresh_token(db: Session) -> str | None:
    row = db.get(FeedlyncAuth, _SINGLETON_ID)
    if row is None or not row.refresh_token:
        return None
    return _decrypt_token(row.refresh_token)


def resolve_refresh_token(db: Session) -> str:
    token = get_stored_refresh_token(db)
    if token:
        return token
    if FEEDLYNC_REFRESH_TOKEN:
        return FEEDLYNC_REFRESH_TOKEN
    raise FeedlyncAuthError(
        "FeedLync is not connected. Use Reconnect FeedLync on the Feed Rate page."
    )


def save_refresh_token(
    db: Session,
    refresh_token: str,
    *,
    user_id: int | None = None,
) -> None:
    token = refresh_token.strip()
    if not token:
        raise ValueError("Refresh token must not be empty.")

    encrypted = _encrypt_token(token)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    row = db.get(FeedlyncAuth, _SINGLETON_ID)
    if row is None:
        db.add(
            FeedlyncAuth(
                id=_SINGLETON_ID,
                refresh_token=encrypted,
                connected_at=now,
                updated_at=now,
                connected_by_user_id=user_id,
            )
        )
    else:
        row.refresh_token = encrypted
        row.updated_at = now
        if user_id is not None:
            row.connected_by_user_id = user_id
    db.commit()


def get_connection_status(db: Session) -> dict[str, object]:
    row = db.get(FeedlyncAuth, _SINGLETON_ID)
    has_env_fallback = bool(FEEDLYNC_REFRESH_TOKEN)
    connected = row is not None or has_env_fallback
    return {
        "connected": connected,
        "source": "database" if row is not None else ("env" if has_env_fallback else None),
        "connected_at": row.connected_at.isoformat() if row and row.connected_at else None,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


def seed_refresh_token_from_env(db: Session) -> bool:
    if not FEEDLYNC_REFRESH_TOKEN:
        return False
    existing = db.scalar(select(FeedlyncAuth.id).where(FeedlyncAuth.id == _SINGLETON_ID))
    if existing is not None:
        return False
    save_refresh_token(db, FEEDLYNC_REFRESH_TOKEN, user_id=None)
    return True
