"""Persist Aged Payables colour marks so they survive Xero invoice rebuilds."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import XeroAgedPayableMarks

CONTACT_ID_PREFIX = "id:"
FARM_SCOPE = "farm"
_VALID_STATUS = frozenset({"critical", "standing_order"})
_AMOUNT_EPS = 0.00499


def empty_payload() -> dict[str, Any]:
    return {"contact_status": {}, "selections": {}}


def contact_keys(contact: str | None, contact_ids: list[str] | None = None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw in contact_ids or []:
        contact_id = str(raw or "").strip()
        if not contact_id:
            continue
        key = f"{CONTACT_ID_PREFIX}{contact_id}"
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    name = str(contact or "").strip()
    if name and name not in seen:
        keys.append(name)
    return keys


def _clean_status_map(raw: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for view, value in raw.items():
        view_id = str(view or "").strip()
        if not view_id or not isinstance(value, dict):
            continue
        mapped: dict[str, str] = {}
        for contact, status in value.items():
            key = str(contact or "").strip()
            flag = str(status or "").strip()
            if key and flag in _VALID_STATUS:
                mapped[key] = flag
        if mapped:
            out[view_id] = mapped
    return out


def _clean_selection_sets(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for view, value in raw.items():
        view_id = str(view or "").strip()
        if not view_id:
            continue
        items = value if isinstance(value, list) else []
        seen: set[str] = set()
        keys: list[str] = []
        for item in items:
            key = str(item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
        if keys:
            out[view_id] = keys
    return out


def normalize_payload(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "contact_status": _clean_status_map(data.get("contact_status")),
        "selections": _clean_selection_sets(data.get("selections")),
    }


def merge_payloads(base: Any, extra: Any) -> tuple[dict[str, Any], bool]:
    """Fill missing keys from extra without overwriting base."""
    merged = normalize_payload(base)
    incoming = normalize_payload(extra)
    changed = False
    for view, mapping in incoming["contact_status"].items():
        bucket = merged["contact_status"].setdefault(view, {})
        for key, status in mapping.items():
            if key not in bucket:
                bucket[key] = status
                changed = True
    for view, keys in incoming["selections"].items():
        bucket = merged["selections"].setdefault(view, [])
        seen = set(bucket)
        for key in keys:
            if key in seen:
                continue
            bucket.append(key)
            seen.add(key)
            changed = True
    return merged, changed


def status_for_contact(
    payload: dict[str, Any],
    *,
    view: str,
    contact: str | None,
    contact_ids: list[str] | None = None,
) -> str:
    mapping = (normalize_payload(payload)["contact_status"] or {}).get(view) or {}
    for key in contact_keys(contact, contact_ids):
        status = mapping.get(key) or ""
        if status:
            return status
    return ""


def is_selected(
    payload: dict[str, Any],
    *,
    view: str,
    contact: str | None,
    month_key: str,
    contact_ids: list[str] | None = None,
) -> bool:
    selected = set((normalize_payload(payload)["selections"] or {}).get(view) or [])
    month = str(month_key or "").strip()
    if not month:
        return False
    return any(f"{key}\t{month}" in selected for key in contact_keys(contact, contact_ids))


def bind_marks_to_contacts(
    payload: Any,
    contacts: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], bool]:
    """Copy name-based marks onto contact-id keys and id marks onto current names."""
    merged = normalize_payload(payload)
    changed = False
    rows = contacts or []
    for row in rows:
        name = str(row.get("contact") or "").strip()
        ids = [str(item).strip() for item in (row.get("contact_ids") or []) if str(item).strip()]
        if row.get("contact_id"):
            contact_id = str(row.get("contact_id") or "").strip()
            if contact_id and contact_id not in ids:
                ids.append(contact_id)
        keys = contact_keys(name, ids)
        if len(keys) < 2:
            continue
        for view, mapping in list(merged["contact_status"].items()):
            status = ""
            for key in keys:
                status = mapping.get(key) or ""
                if status:
                    break
            if not status:
                continue
            for key in keys:
                if mapping.get(key) != status:
                    mapping[key] = status
                    changed = True
        for view, selected in list(merged["selections"].items()):
            selected_set = set(selected)
            months: set[str] = set()
            for item in selected:
                sep = item.find("\t")
                if sep < 0:
                    continue
                key, month = item[:sep], item[sep + 1 :]
                if key in keys and month:
                    months.add(month)
            if not months:
                continue
            for month in months:
                for key in keys:
                    cell = f"{key}\t{month}"
                    if cell not in selected_set:
                        selected.append(cell)
                        selected_set.add(cell)
                        changed = True
    return merged, changed


def prune_zero_selections(
    payload: Any,
    contacts: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], bool]:
    """Drop To Pay marks for paid-off cells and contacts that have left the grid."""
    merged = normalize_payload(payload)
    key_to_row: dict[str, dict[str, Any]] = {}
    folded_to_row: dict[str, dict[str, Any]] = {}
    for row in contacts or []:
        name = str(row.get("contact") or "").strip()
        ids = [str(item).strip() for item in (row.get("contact_ids") or []) if str(item).strip()]
        if row.get("contact_id"):
            contact_id = str(row.get("contact_id") or "").strip()
            if contact_id and contact_id not in ids:
                ids.append(contact_id)
        for key in contact_keys(name, ids):
            key_to_row[key] = row
        if name:
            folded_to_row[name.casefold()] = row

    changed = False
    pruned: dict[str, list[str]] = {}
    for view, selected in merged["selections"].items():
        kept: list[str] = []
        for item in selected:
            sep = item.find("\t")
            if sep < 0:
                changed = True
                continue
            key, month = item[:sep], item[sep + 1 :]
            row = key_to_row.get(key)
            if row is None and not key.startswith(CONTACT_ID_PREFIX):
                row = folded_to_row.get(key.strip().casefold())
            remaining = abs(float(((row or {}).get("amounts") or {}).get(month) or 0.0))
            if row is None or remaining <= _AMOUNT_EPS:
                changed = True
                continue
            kept.append(item)
        if kept:
            pruned[view] = kept
        elif selected:
            changed = True
    merged["selections"] = pruned
    return merged, changed


def _get_row(db: Session) -> XeroAgedPayableMarks | None:
    return db.scalar(
        select(XeroAgedPayableMarks).where(XeroAgedPayableMarks.scope == FARM_SCOPE)
    )


def load_marks(db: Session) -> dict[str, Any]:
    row = _get_row(db)
    return normalize_payload(row.payload if row is not None else None)


def save_marks(
    db: Session,
    payload: Any,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    cleaned = normalize_payload(payload)
    row = _get_row(db)
    if row is None:
        row = XeroAgedPayableMarks(scope=FARM_SCOPE, payload=cleaned)
        db.add(row)
    else:
        row.payload = cleaned
        flag_modified(row, "payload")
    row.updated_by_user_id = user_id
    db.commit()
    db.refresh(row)
    return normalize_payload(row.payload)


def merge_and_save_marks(
    db: Session,
    extra: Any,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    merged, changed = merge_payloads(load_marks(db), extra)
    if changed or _get_row(db) is None:
        return save_marks(db, merged, user_id=user_id)
    return merged
