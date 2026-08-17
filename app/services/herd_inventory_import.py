"""Import herd inventory from DCEXPORT CMINV / GADINV CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import HerdInventory, PedigreeRegistrationRecord
from app.services.graph_onedrive import (
    download_herd_file,
    graph_is_configured,
    herd_file_meta,
)
from app.services.herd_import_utils import (
    FP_INVENTORY,
    bulk_insert_dataframe,
    load_source_fingerprint,
    parse_date_series,
    source_fingerprint,
    store_source_fingerprint,
)
from app.services.inventory_processor import load_inventory_csv, process_inventory_file

logger = logging.getLogger(__name__)

CM_INVENTORY_FILE = "DCEXPORTCM/CMINV.CSV"
GAD_INVENTORY_FILE = "DCEXPORTGAD/GADINV.CSV"
_INVENTORY_FILES = (
    (CM_INVENTORY_FILE, "CM"),
    (GAD_INVENTORY_FILE, "GAD"),
)


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    df = df.reset_index(drop=True)

    def _empty() -> pd.Series:
        return pd.Series([None] * len(df), index=df.index, dtype="object")

    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_date(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return parse_date_series(df[col]).dt.date.replace({pd.NaT: None})

    def series_int(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce").astype("Int64")

    def series_float(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "edat": series_date("EDAT"),
            "cbrd": series_float("CBRD"),
            "sbrd": series_str("SBRD"),
            "fdat": series_date("FDAT"),
            "dim": series_float("DIM"),
            "lact": series_float("LACT"),
            "hdat": series_date("HDAT"),
            "dslh": series_float("DSLH"),
            "rc": series_float("RC"),
            "rpro": series_str("RPRO"),
            "pen": series_str("PEN"),
            "tbrd": series_int("TBRD"),
            "remark": series_str("REMARK"),
            "ewgt": series_float("EWGT"),
            "httag": series_str("HTTAG"),
            "rum": series_float("RUM"),
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


def _sync_pedigree_records(db: Session, *, farm: str | None = None) -> int:
    """Upsert pedigree flags from current inventory snapshot (does not clear registrations)."""
    stmt = select(
        HerdInventory.farm,
        HerdInventory.etag,
        HerdInventory.cow_id,
        HerdInventory.ped,
        HerdInventory.dped,
        HerdInventory.dreg,
        HerdInventory.sreg,
        HerdInventory.sid,
    ).where(HerdInventory.etag.isnot(None)).where(HerdInventory.etag != "")
    if farm:
        stmt = stmt.where(HerdInventory.farm == farm.upper())
    rows = db.execute(stmt).all()

    existing_stmt = select(PedigreeRegistrationRecord)
    if farm:
        existing_stmt = existing_stmt.where(
            PedigreeRegistrationRecord.farm == farm.upper()
        )
    existing = {
        (record.farm, record.etag): record
        for record in db.scalars(existing_stmt).all()
    }
    synced = 0
    for row_farm, etag, cow_id, ped, dped, dreg, sreg, sid in rows:
        etag_norm = (etag or "").strip()
        if not etag_norm:
            continue
        record = existing.get((row_farm, etag_norm))
        if record is None:
            record = PedigreeRegistrationRecord(farm=row_farm, etag=etag_norm)
            db.add(record)
            existing[(row_farm, etag_norm)] = record
        record.cow_id = (cow_id or "").strip() or None
        record.ped = int(ped) if ped is not None else None
        record.dped = int(dped) if dped is not None else None
        record.dreg = (dreg or "").strip() or None
        record.sreg = (sreg or "").strip() or None
        record.sid = (sid or "").strip() or None
        synced += 1
    return synced


# Backward-compatible aliases used by tests.
_farm_fingerprint = source_fingerprint


def _load_farm_fingerprint(db: Session, farm: str) -> str | None:
    return load_source_fingerprint(db, FP_INVENTORY, farm)


def _store_farm_fingerprint(db: Session, farm: str, fingerprint: str) -> None:
    store_source_fingerprint(db, FP_INVENTORY, farm, fingerprint)


def _import_farm_file(
    db: Session, relative_path: str, farm: str, import_time: dt.datetime
) -> int:
    file_bytes = download_herd_file(relative_path)
    df = load_inventory_csv(file_bytes)
    del file_bytes

    df = process_inventory_file(df, farm)
    rows = len(df)
    if rows == 0:
        del df
        gc.collect()
        return 0
    db.execute(delete(HerdInventory).where(HerdInventory.farm == farm))
    bulk_insert_dataframe(db, HerdInventory, df, _dataframe_to_mappings, import_time)
    del df
    gc.collect()
    return rows


def import_herd_inventory(db: Session, *, force: bool = True) -> dict[str, Any]:
    """Download CM / GAD inventory CSVs and replace those farms' inventory rows.

    When ``force=False``, each farm is checked independently against its stored
    file fingerprint. Unchanged farms are left alone; only changed farms are
    downloaded and replaced. Manual / full-herd imports keep ``force=True``.
    """
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    sources: list[dict[str, str]] = []
    farms_imported: list[str] = []
    farms_skipped: list[str] = []
    empty_source_farms: list[str] = []
    rows_imported = 0
    pedigree_synced = 0

    for relative_path, farm in _INVENTORY_FILES:
        meta = herd_file_meta(relative_path)
        last_modified = meta.get("last_modified") or ""
        fingerprint = source_fingerprint(meta["relative_path"], last_modified)
        sources.append(
            {
                "farm": farm,
                "source_file": meta["relative_path"],
                "last_modified": last_modified,
            }
        )

        if not force and last_modified:
            stored = _load_farm_fingerprint(db, farm)
            if stored == fingerprint:
                farms_skipped.append(farm)
                logger.info("Herd inventory %s unchanged; skipping", farm)
                continue

        rows = _import_farm_file(db, relative_path, farm, import_time)
        if rows == 0:
            empty_source_farms.append(farm)
            logger.error(
                "Herd inventory %s file had 0 usable rows; leaving existing data",
                farm,
            )
            continue

        rows_imported += rows
        pedigree_synced += _sync_pedigree_records(db, farm=farm)
        _store_farm_fingerprint(db, farm, fingerprint)
        farms_imported.append(farm)

    if farms_imported:
        db.commit()

    farm_counts = dict(
        db.execute(
            select(HerdInventory.farm, func.count()).group_by(HerdInventory.farm)
        ).all()
    )
    source_files = [item["source_file"] for item in sources]
    all_skipped = bool(farms_skipped) and not farms_imported

    if all_skipped:
        row_count = sum(int(v) for v in farm_counts.values())
        return {
            "skipped": True,
            "reason": "source_unchanged",
            "rows_imported": row_count,
            "pedigree_records_synced": 0,
            "farm_counts": farm_counts,
            "farms_imported": [],
            "farms_skipped": farms_skipped,
            "empty_source_farms": empty_source_farms,
            "imported_at": None,
            "source_files": source_files,
            "sources": sources,
        }

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "pedigree_records_synced": pedigree_synced,
        "farm_counts": farm_counts,
        "farms_imported": farms_imported,
        "farms_skipped": farms_skipped,
        "empty_source_farms": empty_source_farms,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": source_files,
        "sources": sources,
    }
