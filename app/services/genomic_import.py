"""Import genomic evaluation data from DCEXPORTCM/genomicresults.xlsx."""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import GenomicResult
from app.services.graph_onedrive import download_herd_file, graph_is_configured

GENOMIC_FILE = "DCEXPORTCM/genomicresults.xlsx"
GENOMIC_SHEET = "Herd GBR Females"

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


def import_genomic_results(db: Session) -> dict[str, Any]:
    """Download genomicresults.xlsx and replace genomic_results table."""
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    file_bytes = download_herd_file(GENOMIC_FILE)
    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=GENOMIC_SHEET)
    del file_bytes

    import_time = dt.datetime.now()
    db.execute(delete(GenomicResult))
    mappings = _dataframe_to_mappings(df, import_time)
    del df
    if mappings:
        for start in range(0, len(mappings), 2000):
            db.bulk_insert_mappings(GenomicResult, mappings[start : start + 2000])
    rows_imported = len(mappings)
    db.commit()

    return {
        "rows_imported": rows_imported,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_file": GENOMIC_FILE,
        "sheet": GENOMIC_SHEET,
    }
