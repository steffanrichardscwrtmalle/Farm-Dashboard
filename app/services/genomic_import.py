"""Import genomic evaluation data from DCEXPORTCM/genomicresults.xlsx."""

from __future__ import annotations

import datetime as dt
import gc
import io
import json
import re
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AppSetting, GenomicResult
from app.services.graph_onedrive import (
    download_herd_file,
    find_newest_herd_file_meta,
    graph_is_configured,
)

# Newest .xlsx in this folder is used (filename varies between exports).
GENOMIC_FOLDER = "Genomic Results"
GENOMIC_SHEET = "Herd GBR Females"
GENOMIC_SOURCE_SETTING_KEY = "genomic_results.source_fingerprint"

# Excel column name -> GenomicResult field
TRAIT_COLUMNS: dict[str, str] = {
    "Milk": "milk_kg",
    "Fat": "fat_kg",
    "Protein": "protein_kg",
    "Fat %": "fat_pct",
    "Protein %": "protein_pct",
    "PLI": "pli",
    "CCI": "cci",
    "FI": "fertility_index",
    "SCC": "scc",
    "Life Span": "life_span",
    "Mastitis": "mastitis",
    "Milking Speed": "milking_speed",
    "Type Merit": "type_merit",
    "Mammary": "mammary",
    "Legs and Feet": "legs_and_feet",
    "Stature": "stature",
    "Chest Width": "chest_width",
    "Body Depth": "body_depth",
    "Mature Weight": "mature_weight",
}


def normalize_hbn(value: Any) -> str | None:
    """Normalize HBN / ear-tag digits to a comparable string key."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            pass
    digits = re.sub(r"\D", "", str(value).strip())
    return digits or None


def _fingerprint(source_file: str, last_modified: str) -> str:
    return json.dumps(
        {"source_file": source_file, "last_modified": last_modified},
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_stored_fingerprint(db: Session) -> str | None:
    row = db.scalar(
        select(AppSetting).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY)
    )
    value = (row.value if row else None) or ""
    return value.strip() or None


def _store_fingerprint(db: Session, fingerprint: str) -> None:
    row = db.scalar(
        select(AppSetting).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY)
    )
    if row is None:
        db.add(AppSetting(key=GENOMIC_SOURCE_SETTING_KEY, value=fingerprint))
    else:
        row.value = fingerprint


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_float(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce")

    out: dict[str, pd.Series] = {
        "hbn": df["HBN"].map(normalize_hbn),
        "eartag": series_str("EarTag Number"),
        "sire_name": series_str("Sire"),
        "sire_reg": series_str("Sire Reg No ID"),
        "updated_at": pd.Series([import_time] * len(df)),
    }
    for excel_col, field in TRAIT_COLUMNS.items():
        out[field] = series_float(excel_col)

    frame = pd.DataFrame(out)
    frame = frame[frame["hbn"].notna() & (frame["hbn"] != "")]
    frame = frame.drop_duplicates(subset=["hbn"], keep="last")
    return frame.to_dict(orient="records")


def import_genomic_results(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Download the newest genomic results workbook and replace genomic_results.

    Skips download/replace when the newest file path and last-modified timestamp
    match the fingerprint stored from the previous successful import, unless
    ``force=True``.
    """
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    meta = find_newest_herd_file_meta(GENOMIC_FOLDER, suffix=".xlsx")
    source_file = meta["relative_path"]
    last_modified = meta.get("last_modified") or ""
    fingerprint = _fingerprint(source_file, last_modified)

    if not force and last_modified:
        stored = _load_stored_fingerprint(db)
        if stored == fingerprint:
            row_count = db.scalar(select(func.count()).select_from(GenomicResult)) or 0
            return {
                "skipped": True,
                "reason": "source_unchanged",
                "rows_imported": int(row_count),
                "imported_at": None,
                "source_file": source_file,
                "last_modified": last_modified,
                "sheet": GENOMIC_SHEET,
            }

    file_bytes = download_herd_file(source_file)
    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=GENOMIC_SHEET)
    del file_bytes

    import_time = dt.datetime.now()
    db.execute(delete(GenomicResult))
    rows_imported = 0
    for start in range(0, len(df), 2000):
        # reset_index so the helper's fresh 0..n Series align with the slice.
        batch = df.iloc[start : start + 2000].reset_index(drop=True)
        mappings = _dataframe_to_mappings(batch, import_time)
        if mappings:
            db.bulk_insert_mappings(GenomicResult, mappings)
        rows_imported += len(mappings)
        del mappings, batch
    del df
    gc.collect()
    _store_fingerprint(db, fingerprint)
    db.commit()

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_file": source_file,
        "last_modified": last_modified,
        "sheet": GENOMIC_SHEET,
    }
