"""Role definitions and permission helpers."""

from __future__ import annotations

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

ROLE_LABELS: dict[str, str] = {
    ROLE_ADMIN: "Admin",
    ROLE_EDITOR: "Editor",
    ROLE_VIEWER: "Viewer",
}


def is_admin(role: str) -> bool:
    return role == ROLE_ADMIN


def can_edit(role: str) -> bool:
    return role in (ROLE_ADMIN, ROLE_EDITOR)
