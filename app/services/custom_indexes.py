"""Farm-specific £DP and £FW bull indexes (from the breeding spreadsheet)."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting

INDEX_SETTINGS_KEY = "genetics.bull_index_settings"

DEFAULT_INDEX_SETTINGS: dict[str, Any] = {
    "ebv_conv": 2,
    "fertility_weight": 6,
    "lifespan_weight": 0.2,
    "scc_value": -2.2514,
    "mastitis_weight": 4.5,
    "include_mastitis": False,
    "dp": {
        "fat_pct_base": 4.29,
        "protein_pct_base": 3.36,
        "milk_volume_base": 9000,
        "fat_price": 2.9,
        "protein_price": 6.6,
        "volume_price": 6.2,
        "lameness_weight": 2.5,
        "include_lameness": True,
    },
    "fw": {
        "fat_pct_base": 4.0,
        "protein_pct_base": 3.4,
        "milk_volume_base": 13000,
        "fat_price": 2.5,
        "protein_price": 0.0,
        "volume_price": 40.0,
        "lameness_weight": 2.5,
        "include_lameness": False,
    },
}

_SHARED_KEYS = (
    "ebv_conv",
    "fertility_weight",
    "lifespan_weight",
    "scc_value",
    "mastitis_weight",
    "include_mastitis",
)
_SCHEME_KEYS = (
    "fat_pct_base",
    "protein_pct_base",
    "milk_volume_base",
    "fat_price",
    "protein_price",
    "volume_price",
    "lameness_weight",
    "include_lameness",
)


def _get(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def merge_index_settings(raw: Any = None) -> dict[str, Any]:
    settings = deepcopy(DEFAULT_INDEX_SETTINGS)
    if not isinstance(raw, dict):
        return settings
    for key in _SHARED_KEYS:
        if key not in raw:
            continue
        if key == "include_mastitis":
            settings[key] = _bool(raw[key], settings[key])
        else:
            settings[key] = _num(raw[key], settings[key])
    for scheme in ("dp", "fw"):
        incoming = raw.get(scheme)
        if not isinstance(incoming, dict):
            continue
        for key in _SCHEME_KEYS:
            if key not in incoming:
                continue
            if key == "include_lameness":
                settings[scheme][key] = _bool(incoming[key], settings[scheme][key])
            else:
                settings[scheme][key] = _num(incoming[key], settings[scheme][key])
    return settings


def load_index_settings(db: Session) -> dict[str, Any]:
    row = db.scalar(select(AppSetting).where(AppSetting.key == INDEX_SETTINGS_KEY))
    if row is None or not (row.value or "").strip():
        return deepcopy(DEFAULT_INDEX_SETTINGS)
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(DEFAULT_INDEX_SETTINGS)
    return merge_index_settings(parsed)


def save_index_settings(db: Session, raw: Any) -> dict[str, Any]:
    settings = merge_index_settings(raw)
    payload = json.dumps(settings, ensure_ascii=False)
    row = db.scalar(select(AppSetting).where(AppSetting.key == INDEX_SETTINGS_KEY))
    if row is None:
        db.add(AppSetting(key=INDEX_SETTINGS_KEY, value=payload))
    else:
        row.value = payload
    db.commit()
    return settings


def reset_index_settings(db: Session) -> dict[str, Any]:
    row = db.scalar(select(AppSetting).where(AppSetting.key == INDEX_SETTINGS_KEY))
    if row is not None:
        db.delete(row)
        db.commit()
    return deepcopy(DEFAULT_INDEX_SETTINGS)


def _production_indexes(
    *,
    milk_pta: float,
    fatpct_pta: float,
    proteinpct_pta: float,
    fat_pct_base: float,
    protein_pct_base: float,
    milk_volume_base: float,
    fat_price: float,
    protein_price: float,
    volume_price: float,
    ebv_conv: float,
) -> tuple[float, float, float]:
    milk_index = milk_pta * volume_price * ebv_conv / 100
    fat_index = (
        (
            (fatpct_pta + fat_pct_base) * milk_pta * fat_price
            + milk_volume_base * fatpct_pta * fat_price
        )
        / 100
        * ebv_conv
    )
    protein_index = (
        (
            (proteinpct_pta + protein_pct_base) * milk_pta * protein_price
            + milk_volume_base * proteinpct_pta * protein_price
        )
        / 100
        * ebv_conv
    )
    return milk_index, fat_index, protein_index


def _scheme_index(row: Any, settings: dict[str, Any], scheme: str) -> float:
    cfg = settings[scheme]
    ebv = _num(settings["ebv_conv"], 2)
    milk, fat, protein = _production_indexes(
        milk_pta=_num(_get(row, "milk_kg")),
        fatpct_pta=_num(_get(row, "fat_pct")),
        proteinpct_pta=_num(_get(row, "protein_pct")),
        fat_pct_base=_num(cfg["fat_pct_base"]),
        protein_pct_base=_num(cfg["protein_pct_base"]),
        milk_volume_base=_num(cfg["milk_volume_base"]),
        fat_price=_num(cfg["fat_price"]),
        protein_price=_num(cfg["protein_price"]),
        volume_price=_num(cfg["volume_price"]),
        ebv_conv=ebv,
    )
    fertility = _num(_get(row, "fertility_index")) * _num(settings["fertility_weight"]) * ebv
    lifespan = _num(_get(row, "lifespan_days")) * _num(settings["lifespan_weight"]) * ebv
    scc_index = _num(_get(row, "scc")) * _num(settings["scc_value"])
    total = milk + protein + fat + fertility + lifespan + scc_index
    if settings["include_mastitis"]:
        total += _num(_get(row, "mastitis")) * _num(settings["mastitis_weight"]) * ebv
    if cfg["include_lameness"]:
        total += _num(_get(row, "lameness")) * _num(cfg["lameness_weight"]) * ebv
    return total


def dp_index(row: Any, settings: dict[str, Any] | None = None) -> float:
    return _scheme_index(row, merge_index_settings(settings), "dp")


def fw_index(row: Any, settings: dict[str, Any] | None = None) -> float:
    return _scheme_index(row, merge_index_settings(settings), "fw")


def attach_custom_indexes(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = merge_index_settings(settings)
    payload["dp_index"] = round(dp_index(payload, cfg), 2)
    payload["fw_index"] = round(fw_index(payload, cfg), 2)
    return payload
