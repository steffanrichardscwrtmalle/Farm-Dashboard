"""Import herd inventory from DCEXPORT CMINV / GADINV CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AppSetting, HerdInventory, PedigreeRegistrationRecord
from app.services.graph_onedrive import (
    download_herd_file,
    graph_is_configured,
    herd_file_meta,
)
from app.services.herd_import_utils import bulk_insert_dataframe, parse_date_series
from app.services.inventory_processor import load_inventory_csv, process_inventory_file

logger = logging.getLogger(__name__)

CM_INVENTORY_FILE = "DCEXPORTCM/CMINV.CSV"
GAD_INVENTORY_FILE = "DCEXPORTGAD/GADINV.CSV"
INVENTORY_SOURCE_SETTING_KEY = "herd_inventory.source_fingerprint"
_INVENTORY_FILES = (
    (CM_INVENTORY_FILE, "CM"),
    (GAD_INVENTORY_FILE, "GAD"),
)


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_date(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return parse_date_series(df[col]).dt.date.replace({pd.NaT: None})

    def series_int(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce").astype("Int64")

    def series_float(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "cbrd": series_float("CBRD"),
            "sbrd": series_str("SBRD"),
            "fdat": series_date("FDAT"),
            "dim": series_float("DIM"),
            "lact": series_float("LACT"),
            "hdat": series_date("HDAT"),
            "dslh": series_float("DSLH"),
            "rc": series_float("RC"),
            "rpro": series_str("RPRO"),
            "dcc": series_float("DCC"),
            "due": series_date("DUE"),
            "lsir": series_str("LSIR"),
            "sirc": series_str("SIRC"),
            "lsbrd": series_str("LSBRD"),
            "farm": series_str("Farm"),
            "category": series_str("Category"),
            "gender": series_str("Gender"),
            "aged": series_int("AGED"),
            "months_old": series_int("Months Old"),
            "expected_due": series_date("Expected Due"),
            "fiscal_year_due": series_int("Fiscal Year Due"),
            "sort_key": series_int("Sort Key"),
            "expected_month": series_str("Expected Month"),
            "value": series_float("Value"),
            "ped": series_int("PED"),
            "dped": series_int("DPED"),
            "dreg": series_str("DREG"),
            "sreg": series_str("SREG"),
            "sid": series_str("SID"),
            "gid": series_str("GID"),
            "gtest": series_date("GTEST"),
            "subd": series_date("SUBD"),
            "import_timestamp": import_time,
        }
    )
    return out.to_dict(orient="records")


def _sync_pedigree_records(db: Session) -> int:
    """Upsert pedigree flags from current inventory snapshot (does not clear registrations)."""
    rows = db.execute(
        select(
            HerdInventory.farm,
            HerdInventory.etag,
            HerdInventory.cow_id,
            HerdInventory.ped,
            HerdInventory.dped,
            HerdInventory.dreg,
            HerdInventory.sreg,
            HerdInventory.sid,
        ).where(HerdInventory.etag.isnot(None)).where(HerdInventory.etag != "")
    ).all()

    existing = {
        (record.farm, record.etag): record
        for record in db.scalars(select(PedigreeRegistrationRecord)).all()
    }
    synced = 0
    for farm, etag, cow_id, ped, dped, dreg, sreg, sid in rows:
        etag_norm = (etag or "").strip()
        if not etag_norm:
            continue
        record = existing.get((farm, etag_norm))
        if record is None:
            record = PedigreeRegistrationRecord(farm=farm, etag=etag_norm)
            db.add(record)
            existing[(farm, etag_norm)] = record
        record.cow_id = (cow_id or "").strip() or None
        record.ped = int(ped) if ped is not None else None
        record.dped = int(dped) if dped is not None else None
        record.dreg = (dreg or "").strip() or None
        record.sreg = (sreg or "").strip() or None
        record.sid = (sid or "").strip() or None
        synced += 1
    return synced


def _fingerprint_sources(sources: list[dict[str, str]]) -> str:
    return json.dumps({"files": sources}, separators=(",", ":"), sort_keys=True)


def _load_stored_fingerprint(db: Session) -> str | None:
    row = db.scalar(
        select(AppSetting).where(AppSetting.key == INVENTORY_SOURCE_SETTING_KEY)
    )
    value = (row.value if row else None) or ""
    return value.strip() or None


def _store_fingerprint(db: Session, fingerprint: str) -> None:
    row = db.scalar(
        select(AppSetting).where(AppSetting.key == INVENTORY_SOURCE_SETTING_KEY)
    )
    if row is None:
        db.add(AppSetting(key=INVENTORY_SOURCE_SETTING_KEY, value=fingerprint))
    else:
        row.value = fingerprint


def _inventory_source_metas() -> list[dict[str, str]]:
    metas: list[dict[str, str]] = []
    for relative_path, _farm in _INVENTORY_FILES:
        meta = herd_file_meta(relative_path)
        metas.append(
            {
                "source_file": meta["relative_path"],
                "last_modified": meta.get("last_modified") or "",
            }
        )
    return metas


def _import_farm_file(
    db: Session, relative_path: str, farm: str, import_time: dt.datetime
) -> int:
    file_bytes = download_herd_file(relative_path)
    df = load_inventory_csv(file_bytes)
    del file_bytes

    df = process_inventory_file(df, farm)
    bulk_insert_dataframe(db, HerdInventory, df, _dataframe_to_mappings, import_time)
    rows = len(df)
    del df
    gc.collect()
    return rows


def import_herd_inventory(db: Session, *, force: bool = True) -> dict[str, Any]:
    """Download CM + GAD inventory CSVs, clean, and replace herd_inventory table.

    When ``force=False``, skips download/replace if both source files' path +
    last-modified fingerprints match the previous successful import (same idea
    as genomic results). Manual / full-herd imports keep ``force=True``.
    """
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    sources = _inventory_source_metas()
    fingerprint = _fingerprint_sources(sources)
    source_files = [item["source_file"] for item in sources]
    all_have_mtime = all(item.get("last_modified") for item in sources)

    if not force and all_have_mtime:
        stored = _load_stored_fingerprint(db)
        if stored == fingerprint:
            row_count = db.scalar(select(func.count()).select_from(HerdInventory)) or 0
            farm_counts = dict(
                db.execute(
                    select(HerdInventory.farm, func.count()).group_by(HerdInventory.farm)
                ).all()
            )
            logger.info("Herd inventory unchanged; skipping import")
            return {
                "skipped": True,
                "reason": "source_unchanged",
                "rows_imported": int(row_count),
                "pedigree_records_synced": 0,
                "farm_counts": farm_counts,
                "imported_at": None,
                "source_files": source_files,
                "sources": sources,
            }

    import_time = dt.datetime.now()
    db.execute(delete(HerdInventory))

    rows_imported = 0
    for relative_path, farm in _INVENTORY_FILES:
        rows_imported += _import_farm_file(db, relative_path, farm, import_time)

    pedigree_synced = _sync_pedigree_records(db)
    _store_fingerprint(db, fingerprint)
    db.commit()

    farm_counts = dict(
        db.execute(
            select(HerdInventory.farm, func.count()).group_by(HerdInventory.farm)
        ).all()
    )

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "pedigree_records_synced": pedigree_synced,
        "farm_counts": farm_counts,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": source_files,
        "sources": sources,
    }
