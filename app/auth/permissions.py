"""Page and action permission registry and helpers."""

from __future__ import annotations

import json
from typing import Any

from app.auth.roles import is_admin
from app.models import User

PAGE_WYNNSTAY = "wynnstay"
PAGE_PROSTOCK = "prostock"
PAGE_STOCK_INVENTORY = "stock_inventory"
PAGE_BCMS = "bcms"
PAGE_EVENTS = "events"
PAGE_FEED_RATE = "feed_rate"
PAGE_OFFICE_ADMIN = "office_admin"
PAGE_XERO = "xero"
PAGE_GENETICS = "genetics"
PAGE_MILK_QUALITY = "milk_quality"
PAGE_CATTLE_SALES = "cattle_sales"
PAGE_BENCHMARKING = "benchmarking"
PAGE_HR = "hr"
PAGE_PARLOUR = "parlour"

PAGE_KEYS: tuple[str, ...] = (
    PAGE_WYNNSTAY,
    PAGE_PROSTOCK,
    PAGE_STOCK_INVENTORY,
    PAGE_BCMS,
    PAGE_EVENTS,
    PAGE_FEED_RATE,
    PAGE_OFFICE_ADMIN,
    PAGE_XERO,
    PAGE_GENETICS,
    PAGE_MILK_QUALITY,
    PAGE_CATTLE_SALES,
    PAGE_BENCHMARKING,
    PAGE_HR,
    PAGE_PARLOUR,
)

PAGE_LABELS: dict[str, str] = {
    PAGE_WYNNSTAY: "Wynnstay (Suppliers)",
    PAGE_PROSTOCK: "Prostock (Suppliers)",
    PAGE_STOCK_INVENTORY: "Stock Inventory",
    PAGE_BCMS: "BCMS",
    PAGE_EVENTS: "Events",
    PAGE_FEED_RATE: "Feed Rate",
    PAGE_OFFICE_ADMIN: "Office Admin",
    PAGE_XERO: "Xero",
    PAGE_GENETICS: "Genetics",
    PAGE_MILK_QUALITY: "Milk Sales",
    PAGE_CATTLE_SALES: "Cattle Sales",
    PAGE_BENCHMARKING: "Benchmarking",
    PAGE_HR: "Staff / HR",
    PAGE_PARLOUR: "Parlour",
}

ACTION_WYNNSTAY_IMPORT = "wynnstay.import"
ACTION_WYNNSTAY_MAPPINGS = "wynnstay.mappings"
ACTION_PROSTOCK_IMPORT = "prostock.import"
ACTION_PROSTOCK_MAPPINGS = "prostock.mappings"
ACTION_HERD_IMPORT = "herd.import"
ACTION_OFFICE_ADMIN_SALES_PAYMENT = "office_admin.sales_payment"
ACTION_OFFICE_ADMIN_FALLEN_STOCK = "office_admin.fallen_stock"
ACTION_GENETICS_PEDIGREE = "genetics.pedigree"
ACTION_GENETICS_PENDING_RESULTS = "genetics.pending_results"
ACTION_MILK_QUALITY_IMPORT = "milk_quality.import"
ACTION_MILK_COLLECTIONS_IMPORT = "milk_quality.collections_import"
ACTION_MILK_STATEMENTS_IMPORT = "milk_quality.statements_import"
ACTION_CATTLE_SALES_IMPORT = "cattle_sales.import"
ACTION_BENCHMARKING_EDIT = "benchmarking.edit"
ACTION_HR_ENROLL = "hr.enroll"
ACTION_HR_VIEW_SENSITIVE = "hr.view_sensitive"
ACTION_PARLOUR_IMPORT = "parlour.import"
ACTION_CTS_SYNC = "cts.sync"

ACTION_KEYS: tuple[str, ...] = (
    ACTION_WYNNSTAY_IMPORT,
    ACTION_WYNNSTAY_MAPPINGS,
    ACTION_PROSTOCK_IMPORT,
    ACTION_PROSTOCK_MAPPINGS,
    ACTION_HERD_IMPORT,
    ACTION_OFFICE_ADMIN_SALES_PAYMENT,
    ACTION_OFFICE_ADMIN_FALLEN_STOCK,
    ACTION_GENETICS_PEDIGREE,
    ACTION_GENETICS_PENDING_RESULTS,
    ACTION_MILK_QUALITY_IMPORT,
    ACTION_MILK_COLLECTIONS_IMPORT,
    ACTION_MILK_STATEMENTS_IMPORT,
    ACTION_CATTLE_SALES_IMPORT,
    ACTION_BENCHMARKING_EDIT,
    ACTION_HR_ENROLL,
    ACTION_HR_VIEW_SENSITIVE,
    ACTION_PARLOUR_IMPORT,
    ACTION_CTS_SYNC,
)

ACTION_LABELS: dict[str, str] = {
    ACTION_WYNNSTAY_IMPORT: "Wynnstay — import / refresh data",
    ACTION_WYNNSTAY_MAPPINGS: "Wynnstay — edit product mappings",
    ACTION_PROSTOCK_IMPORT: "Prostock — import / refresh data",
    ACTION_PROSTOCK_MAPPINGS: "Prostock — edit product mappings",
    ACTION_HERD_IMPORT: "Herd — import CSV data",
    ACTION_OFFICE_ADMIN_SALES_PAYMENT: "Office Admin — confirm sales payments",
    ACTION_OFFICE_ADMIN_FALLEN_STOCK: "Office Admin — confirm fallen stock collection",
    ACTION_GENETICS_PEDIGREE: "Genetics — pedigree registrations (email & restore)",
    ACTION_GENETICS_PENDING_RESULTS: "Genetics — pending results (email & refresh genomics)",
    ACTION_MILK_QUALITY_IMPORT: "Milk Sales — import NML results from email",
    ACTION_MILK_COLLECTIONS_IMPORT: "Milk Sales — import haulier collections from email",
    ACTION_MILK_STATEMENTS_IMPORT: "Milk Sales — import buyer statements from email",
    ACTION_CATTLE_SALES_IMPORT: "Cattle Sales — import Eurofarm / Pathway / Buitelaar / Game Changer remittances from email",
    ACTION_BENCHMARKING_EDIT: "Benchmarking — edit forecast tables",
    ACTION_HR_ENROLL: "HR — enroll new staff",
    ACTION_HR_VIEW_SENSITIVE: "HR — view sensitive PII (NI, pay details)",
    ACTION_PARLOUR_IMPORT: "Parlour — import milk flow shift reports",
    ACTION_CTS_SYNC: "CTS — sync cattle on holding",
}

ALL_PAGES = list(PAGE_KEYS)
ALL_ACTIONS = list(ACTION_KEYS)

PRESET_FARM_WORKER = "farm_worker"
PRESET_OFFICE = "office"

PRESETS: dict[str, dict[str, Any]] = {
    PRESET_FARM_WORKER: {
        "label": "Farm worker",
        "pages": [PAGE_FEED_RATE, PAGE_EVENTS, PAGE_STOCK_INVENTORY, PAGE_BCMS],
        "actions": [],
    },
    PRESET_OFFICE: {
        "label": "Office",
        "pages": ALL_PAGES,
        "actions": ALL_ACTIONS,
    },
}

DEFAULT_EDITOR_PERMISSIONS: dict[str, list[str]] = {
    "pages": ALL_PAGES,
    "actions": ALL_ACTIONS,
}

DEFAULT_VIEWER_PERMISSIONS: dict[str, list[str]] = {
    "pages": ALL_PAGES,
    "actions": [],
}


def empty_permissions() -> dict[str, list[str]]:
    return {"pages": [], "actions": []}


def parse_permissions(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return empty_permissions()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return empty_permissions()
    if not isinstance(data, dict):
        return empty_permissions()
    pages = data.get("pages") or []
    actions = data.get("actions") or []
    return {
        "pages": [p for p in pages if p in PAGE_KEYS],
        "actions": [a for a in actions if a in ACTION_KEYS],
    }


def serialize_permissions(perms: dict[str, Any] | None) -> str:
    normalized = normalize_permissions(perms)
    return json.dumps(normalized)


def normalize_permissions(perms: dict[str, Any] | None) -> dict[str, list[str]]:
    if not perms:
        return empty_permissions()
    pages = perms.get("pages") or []
    actions = perms.get("actions") or []
    return {
        "pages": sorted({p for p in pages if p in PAGE_KEYS}),
        "actions": sorted({a for a in actions if a in ACTION_KEYS}),
    }


def permissions_for_admin_ui() -> dict[str, Any]:
    return {
        "pages": [{"id": key, "label": PAGE_LABELS[key]} for key in PAGE_KEYS],
        "actions": [{"id": key, "label": ACTION_LABELS[key]} for key in ACTION_KEYS],
        "presets": [
            {"id": preset_id, "label": meta["label"]}
            for preset_id, meta in PRESETS.items()
        ],
    }


def preset_permissions(preset_id: str) -> dict[str, list[str]]:
    meta = PRESETS.get(preset_id)
    if not meta:
        raise ValueError(f"Unknown preset: {preset_id}")
    return normalize_permissions(meta)


def has_page(user: User | None, page_key: str) -> bool:
    if user is None:
        return False
    if is_admin(user.role):
        return True
    pages = parse_permissions(user.permissions).get("pages", [])
    if page_key in pages:
        return True
    # Xero is a top-level section; allow Office Admin users during rollout.
    if page_key == PAGE_XERO and PAGE_OFFICE_ADMIN in pages:
        return True
    # BCMS was previously under Stock Inventory; keep access during rollout.
    if page_key == PAGE_BCMS and PAGE_STOCK_INVENTORY in pages:
        return True
    return False


def has_action(user: User | None, action_key: str) -> bool:
    if user is None:
        return False
    if is_admin(user.role):
        return True
    return action_key in parse_permissions(user.permissions).get("actions", [])


def can_import_feed(user: User | None) -> bool:
    """Any authenticated user may refresh Feedlync feed data."""
    return user is not None and user.is_active


MILK_IMPORT_ACTIONS: tuple[str, ...] = (
    ACTION_MILK_STATEMENTS_IMPORT,
    ACTION_MILK_QUALITY_IMPORT,
    ACTION_MILK_COLLECTIONS_IMPORT,
)


def can_import_milk_statements(user: User | None) -> bool:
    """Import buyer statements if the user may import any milk-sales email data."""
    return any(has_action(user, key) for key in MILK_IMPORT_ACTIONS)


def can_edit_sires(user: User | None) -> bool:
    """Only admins may change bull beef/dairy classification."""
    return user is not None and is_admin(user.role)


class PermissionContext:
    """Template helper for page/action checks."""

    def __init__(self, user: User | None) -> None:
        self._user = user

    def page(self, key: str) -> bool:
        return has_page(self._user, key)

    def action(self, key: str) -> bool:
        return has_action(self._user, key)
