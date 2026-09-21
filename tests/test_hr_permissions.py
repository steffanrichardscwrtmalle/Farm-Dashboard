"""Staff / HR page access does not require Office Admin."""

from __future__ import annotations

from app.auth.permissions import (
    ACTION_HR_ENROLL,
    ACTION_HR_TIMESHEETS,
    ACTION_HR_VIEW_SENSITIVE,
    ACTION_OFFICE_ADMIN_FALLEN_STOCK,
    ACTION_OFFICE_ADMIN_SALES_PAYMENT,
    PAGE_HR,
    PAGE_OFFICE_ADMIN,
    PAGE_XERO,
    PRESET_OFFICE,
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
    assert ACTION_HR_TIMESHEETS in perms["actions"]


def test_office_preset_does_not_grant_everything() -> None:
    perms = preset_permissions(PRESET_OFFICE)
    assert PAGE_OFFICE_ADMIN in perms["pages"]
    assert PAGE_XERO in perms["pages"]
    assert PAGE_HR not in perms["pages"]
    assert ACTION_HR_TIMESHEETS not in perms["actions"]
    assert ACTION_HR_ENROLL not in perms["actions"]
    assert ACTION_HR_VIEW_SENSITIVE not in perms["actions"]
    assert ACTION_OFFICE_ADMIN_SALES_PAYMENT in perms["actions"]
    assert ACTION_OFFICE_ADMIN_FALLEN_STOCK in perms["actions"]


def test_sensehub_refresh_and_cull_are_separate_actions() -> None:
    from app.auth.permissions import (
        ACTION_SENSEHUB_CULL,
        ACTION_SENSEHUB_IMPORT,
        PAGE_SENSEHUB,
    )

    refresh_only = normalize_permissions(
        {"pages": [PAGE_SENSEHUB], "actions": [ACTION_SENSEHUB_IMPORT]}
    )
    assert ACTION_SENSEHUB_IMPORT in refresh_only["actions"]
    assert ACTION_SENSEHUB_CULL not in refresh_only["actions"]

    cull_only = _user('{"pages":["sensehub"],"actions":["sensehub.cull"]}')
    assert has_action(cull_only, ACTION_SENSEHUB_CULL)
    assert not has_action(cull_only, ACTION_SENSEHUB_IMPORT)

    catalog = permissions_for_admin_ui()
    sensehub = next(item for item in catalog["pages"] if item["id"] == PAGE_SENSEHUB)
    action_ids = {item["id"] for item in sensehub["actions"]}
    assert ACTION_SENSEHUB_IMPORT in action_ids
    assert ACTION_SENSEHUB_CULL in action_ids


def test_admin_catalog_nests_hr_actions_under_hr_page() -> None:
    catalog = permissions_for_admin_ui()
    hr = next(item for item in catalog["pages"] if item["id"] == PAGE_HR)
    action_ids = {item["id"] for item in hr["actions"]}
    assert action_ids == {
        ACTION_HR_ENROLL,
        ACTION_HR_VIEW_SENSITIVE,
        ACTION_HR_TIMESHEETS,
    }
    office = next(item for item in catalog["pages"] if item["id"] == PAGE_OFFICE_ADMIN)
    office_ids = {item["id"] for item in office["actions"]}
    assert ACTION_HR_ENROLL not in office_ids
    assert ACTION_HR_VIEW_SENSITIVE not in office_ids
    assert ACTION_HR_TIMESHEETS not in office_ids


def test_timesheets_is_separate_from_directory_access() -> None:
    directory_only = _user('{"pages":["hr"],"actions":[]}')
    assert has_page(directory_only, PAGE_HR)
    assert not has_action(directory_only, ACTION_HR_TIMESHEETS)

    timesheets_only = _user('{"pages":[],"actions":["hr.timesheets"]}')
    assert has_page(timesheets_only, PAGE_HR)
    assert has_action(timesheets_only, ACTION_HR_TIMESHEETS)
    assert not has_action(timesheets_only, ACTION_HR_ENROLL)
    assert not has_action(timesheets_only, ACTION_HR_VIEW_SENSITIVE)
