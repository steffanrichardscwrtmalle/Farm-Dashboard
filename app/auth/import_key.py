"""API key authentication for automated imports (cron / Power Automate)."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request

from app.auth.deps import get_session_user_id
from app.auth.roles import can_edit
from app.config import IMPORT_API_KEY
from app.db import get_db
from app.models import User
from sqlalchemy.orm import Session


def valid_import_key(request: Request) -> bool:
    if not IMPORT_API_KEY:
        return False
    provided = request.headers.get("X-Import-Key", "")
    return secrets.compare_digest(provided, IMPORT_API_KEY)


def require_import_or_editor(
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    """Allow cron (X-Import-Key) or logged-in editor/admin."""
    if valid_import_key(request):
        return

    user_id = get_session_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not can_edit(user.role):
        raise HTTPException(status_code=403, detail="Editor access required")
