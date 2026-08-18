"""Staff / HR page access does not require Office Admin."""

from __future__ import annotations

from app.auth.permissions import (
    ACTION_HR_ENROLL,
    ACTION_HR_VIEW_SENSITIVE,
    PAGE_HR,
    PAGE_OFFICE_ADMIN,
    PRESET_STAFF_HR,
    has_action,
    has_page,
    normalize_permissions,
    parse_permissions,
    permissions_for_admin_ui,
    preset_permissions,
)
from app.models import User


def _user(permissions: str) -> User:
    return User(
        email="hr-only@example.com",
        password_hash="x",
        role="user",
        permissions=permissions,
    )


def test_hr_page_does_not_require_office_admin() -> None:
    user = _user('{"pages":["hr"],"actions":[]}')
    assert has_page(user, PAGE_HR)
    assert not has_page(user, PAGE_OFFICE_ADMIN)


def test_hr_actions_imply_hr_page_without_office_admin() -> None:
    user = _user('{"pages":[],"actions":["hr.enroll","hr.view_sensitive"]}')
    assert has_page(user, PAGE_HR)
    assert has_action(user, ACTION_HR_ENROLL)
    assert has_action(user, ACTION_HR_VIEW_SENSITIVE)
    assert not has_page(user, PAGE_OFFICE_ADMIN)


def test_normalize_adds_hr_page_from_hr_actions() -> None:
    result = normalize_permissions(
        {"pages": [], "actions": [ACTION_HR_ENROLL, ACTION_HR_VIEW_SENSITIVE]}
    )
    assert PAGE_HR in result["pages"]
    assert PAGE_OFFICE_ADMIN not in result["pages"]
    assert ACTION_HR_ENROLL in result["actions"]


def test_parse_permissions_implies_hr_page() -> None:
    parsed = parse_permissions('{"pages":[],"actions":["hr.enroll"]}')
    assert parsed["pages"] == [PAGE_HR]
    assert parsed["actions"] == [ACTION_HR_ENROLL]


def test_staff_hr_preset_excludes_office_admin() -> None:
    perms = preset_permissions(PRESET_STAFF_HR)
    assert PAGE_HR in perms["pages"]
    assert PAGE_OFFICE_ADMIN not in perms["pages"]
    assert ACTION_HR_ENROLL in perms["actions"]
    assert ACTION_HR_VIEW_SENSITIVE in perms["actions"]


def test_admin_catalog_nests_hr_actions_under_hr_page() -> None:
    catalog = permissions_for_admin_ui()
    hr = next(item for item in catalog["pages"] if item["id"] == PAGE_HR)
    action_ids = {item["id"] for item in hr["actions"]}
    assert action_ids == {ACTION_HR_ENROLL, ACTION_HR_VIEW_SENSITIVE}
    office = next(item for item in catalog["pages"] if item["id"] == PAGE_OFFICE_ADMIN)
    office_ids = {item["id"] for item in office["actions"]}
    assert ACTION_HR_ENROLL not in office_ids
    assert ACTION_HR_VIEW_SENSITIVE not in office_ids
