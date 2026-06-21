"""Role definitions and permission helpers."""

from __future__ import annotations

ROLE_ADMIN = "admin"
ROLE_USER = "user"

# Legacy roles migrated to ROLE_USER on startup
LEGACY_ROLE_EDITOR = "editor"
LEGACY_ROLE_VIEWER = "viewer"

ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_USER)

ROLE_LABELS: dict[str, str] = {
    ROLE_ADMIN: "Admin",
    ROLE_USER: "User",
}


def is_admin(role: str) -> bool:
    return role == ROLE_ADMIN


def can_edit(role: str) -> bool:
    """Backward-compatible: admin always; user role uses permissions elsewhere."""
    return role == ROLE_ADMIN or role == LEGACY_ROLE_EDITOR
