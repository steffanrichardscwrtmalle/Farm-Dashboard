"""Milk Statements and Feed Contracts are grantable separately from their parent sections."""

from __future__ import annotations

from app.auth.permissions import (
    ACTION_MILK_STATEMENTS_IMPORT,
    PAGE_FEED_CONTRACTS,
    PAGE_FEED_RATE,
    PAGE_MILK_QUALITY,
    PAGE_MILK_STATEMENTS,
    PRESET_FARM_WORKER,
    has_action,
    has_page,
    normalize_permissions,
    permissions_for_admin_ui,
    preset_permissions,
)
from app.models import User


def _user(permissions: str) -> User:
    return User(
        email="split@example.com",
        password_hash="x",
        role="user",
        permissions=permissions,
    )


def test_milk_statements_page_does_not_require_collections() -> None:
    user = _user('{"pages":["milk_statements"],"actions":[]}')
    assert has_page(user, PAGE_MILK_STATEMENTS)
    assert not has_page(user, PAGE_MILK_QUALITY)


def test_collections_page_does_not_imply_statements() -> None:
    user = _user('{"pages":["milk_quality"],"actions":[]}')
    assert has_page(user, PAGE_MILK_QUALITY)
    assert not has_page(user, PAGE_MILK_STATEMENTS)


def test_statements_import_action_implies_statements_page() -> None:
    result = normalize_permissions(
        {"pages": [], "actions": [ACTION_MILK_STATEMENTS_IMPORT]}
    )
    assert PAGE_MILK_STATEMENTS in result["pages"]
    assert PAGE_MILK_QUALITY not in result["pages"]
    assert ACTION_MILK_STATEMENTS_IMPORT in result["actions"]

    user = _user('{"pages":[],"actions":["milk_quality.statements_import"]}')
    assert has_page(user, PAGE_MILK_STATEMENTS)
    assert has_action(user, ACTION_MILK_STATEMENTS_IMPORT)
    assert not has_page(user, PAGE_MILK_QUALITY)


def test_feed_contracts_page_does_not_require_feed_rations() -> None:
    user = _user('{"pages":["feed_contracts"],"actions":[]}')
    assert has_page(user, PAGE_FEED_CONTRACTS)
    assert not has_page(user, PAGE_FEED_RATE)


def test_feed_rations_page_does_not_imply_contracts() -> None:
    user = _user('{"pages":["feed_rate"],"actions":[]}')
    assert has_page(user, PAGE_FEED_RATE)
    assert not has_page(user, PAGE_FEED_CONTRACTS)


def test_farm_worker_preset_includes_feed_contracts() -> None:
    perms = preset_permissions(PRESET_FARM_WORKER)
    assert PAGE_FEED_RATE in perms["pages"]
    assert PAGE_FEED_CONTRACTS in perms["pages"]


def test_admin_catalog_lists_statements_and_contracts_pages() -> None:
    catalog = permissions_for_admin_ui()
    by_id = {item["id"]: item for item in catalog["pages"]}
    assert PAGE_MILK_STATEMENTS in by_id
    assert PAGE_FEED_CONTRACTS in by_id
    statement_actions = {item["id"] for item in by_id[PAGE_MILK_STATEMENTS]["actions"]}
    assert ACTION_MILK_STATEMENTS_IMPORT in statement_actions
    collections_actions = {item["id"] for item in by_id[PAGE_MILK_QUALITY]["actions"]}
    assert ACTION_MILK_STATEMENTS_IMPORT not in collections_actions
