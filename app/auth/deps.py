"""FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_ADMIN, can_edit, is_admin
from app.db import get_db
from app.models import User


def get_session_user_id(request: Request) -> int | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = get_session_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_editor(user: User = Depends(get_current_user)) -> User:
    if not can_edit(user.role):
        raise HTTPException(status_code=403, detail="Editor access required")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user.role):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = get_session_user_id(request)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user
