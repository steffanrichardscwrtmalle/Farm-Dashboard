"""User persistence helpers."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.auth.permissions import normalize_permissions, serialize_permissions
from app.auth.roles import ROLE_ADMIN, ROLE_USER, ROLES
from app.config import ADMIN_EMAIL, ADMIN_PASSWORD, MIN_PASSWORD_LENGTH
from app.models import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    role: str,
    permissions: dict | None = None,
) -> User:
    validate_password(password)
    validate_role(role)
    normalized = normalize_email(email)
    if get_user_by_email(db, normalized):
        raise ValueError("A user with this email already exists")
    perms_json = None
    if role != ROLE_ADMIN:
        perms_json = serialize_permissions(normalize_permissions(permissions))
    user = User(
        email=normalized,
        password_hash=hash_password(password),
        role=role,
        permissions=perms_json,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_password(db: Session, user: User, password: str) -> None:
    validate_password(password)
    user.password_hash = hash_password(password)
    db.commit()


def seed_admin_user(db: Session) -> User | None:
    """Create the initial admin from ADMIN_EMAIL / ADMIN_PASSWORD if no users exist."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return None
    existing = db.scalar(select(func.count()).select_from(User)) or 0
    if existing:
        return get_user_by_email(db, ADMIN_EMAIL)
    try:
        return create_user(
            db,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            role=ROLE_ADMIN,
        )
    except ValueError:
        return get_user_by_email(db, ADMIN_EMAIL)
