"""API key authentication for automated imports (cron / Power Automate)."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.deps import get_session_user_id
from app.auth.permissions import has_action
from app.config import IMPORT_API_KEY
from app.db import get_db
from app.models import User


def valid_import_key(request: Request) -> bool:
    if not IMPORT_API_KEY:
        return False
    provided = request.headers.get("X-Import-Key", "")
    return secrets.compare_digest(provided, IMPORT_API_KEY)


def require_import_or_action(action_key: str) -> Callable[..., None]:
    """Allow cron (X-Import-Key) or logged-in user with the given action permission."""

    def _dependency(request: Request, db: Session = Depends(get_db)) -> None:
        if valid_import_key(request):
            return

        user_id = get_session_user_id(request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not has_action(user, action_key):
            raise HTTPException(status_code=403, detail="You do not have permission for this action")

    return _dependency


def require_import_or_any_action(*action_keys: str) -> Callable[..., None]:
    """Allow cron (X-Import-Key) or logged-in user with any listed action permission."""

    def _dependency(request: Request, db: Session = Depends(get_db)) -> None:
        if valid_import_key(request):
            return

        user_id = get_session_user_id(request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not any(has_action(user, key) for key in action_keys):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission for this action",
            )

    return _dependency
