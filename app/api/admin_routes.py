"""Admin API routes (user management)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_admin
from app.auth.roles import ROLES, ROLE_LABELS
from app.auth.users import create_user, normalize_email, update_user_password, validate_role
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserCreate(BaseModel):
    email: str
    password: str
    role: str = "viewer"


class AdminUserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12)


@router.get("/users")
def api_list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    users = list(db.scalars(select(User).order_by(User.email)).all())
    return {"items": [u.to_dict() for u in users]}


@router.post("/users")
def api_create_user(
    body: AdminUserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        user = create_user(db, email=body.email, password=body.password, role=body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user.to_dict()


@router.patch("/users/{user_id}")
def api_update_user(
    user_id: int,
    body: AdminUserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        if body.is_active is False:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        if body.role is not None and body.role != admin.role:
            raise HTTPException(status_code=400, detail="You cannot change your own role here")

    if body.role is not None:
        try:
            validate_role(body.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        user.role = body.role

    if body.is_active is not None:
        user.is_active = body.is_active

    if body.password is not None:
        try:
            update_user_password(db, user, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(user)
    return user.to_dict()
