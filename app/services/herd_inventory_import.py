"""Import herd inventory from DCEXPORT CMINV / GADINV CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import HerdInventory
from app.services.graph_onedrive import download_herd_file, graph_is_configured
from app.services.herd_import_utils import (
    HERD_DATE_FORMAT,
    bulk_insert_dataframe,
    drop_unnamed_columns,
    parse_date_series,
    remove_invalid_id_rows,
    strip_string_columns,
)

CM_INVENTORY_FILE = "DCEXPORTCM/CMINV.CSV"
GAD_INVENTORY_FILE = "DCEXPORTGAD/GADINV.CSV"

_INVENTORY_DATE_COLUMNS = ("BDAT", "FDAT", "HDAT", "DUE")
_INVENTORY_ENCODING = "utf-8"

_CATEGORY_MAP = {
    "HEIFER": "Heifer",
    "FRESH": "Fresh",
    "BRED": "Bred",
    "PREG": "Pregnant",
    "DRY": "Dry",
    "DNB": "DNB",
    "OK/OPEN": "Open",
    "BULL": "Bull",
}


def _inventory_category(rpro: Any) -> str | None:
    if rpro is None or (isinstance(rpro, float) and pd.isna(rpro)):
        return None
    key = str(rpro).strip().upper()
    if not key:
        return None
    return _CATEGORY_MAP.get(key, key.title())


def _clean_inventory_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = drop_unnamed_columns(df)
    df = strip_string_columns(df)
    df = remove_invalid_id_rows(df)

    for col in _INVENTORY_DATE_COLUMNS:
        if col in df.columns:
            df[col] = parse_date_series(df[col])

    for col in ("CBRD", "LACT", "DIM", "DSLH", "RC", "DCC"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "RPRO" in df.columns:
        df["Category"] = df["RPRO"].map(_inventory_category)
    else:
        df["Category"] = None

    if "DUE" in df.columns:
        df["Expected Due"] = df["DUE"]
    else:
        df["Expected Due"] = pd.NaT

    return df


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
            "expected_due": series_date("Expected Due"),
            "import_timestamp": import_time,
        }
    )
    return out.to_dict(orient="records")


def _import_farm_file(
    db: Session, relative_path: str, farm: str, import_time: dt.datetime
) -> int:
    file_bytes = download_herd_file(relative_path)
    df = pd.read_csv(
        io.BytesIO(file_bytes),
        encoding=_INVENTORY_ENCODING,
        dayfirst=True,
        on_bad_lines="skip",
        parse_dates=list(_INVENTORY_DATE_COLUMNS),
        date_format=HERD_DATE_FORMAT,
    )
    del file_bytes

    df["Farm"] = farm
    df = _clean_inventory_dataframe(df)
    bulk_insert_dataframe(db, HerdInventory, df, _dataframe_to_mappings, import_time)
    rows = len(df)
    del df
    gc.collect()
    return rows


def import_herd_inventory(db: Session) -> dict[str, Any]:
    """Download CM + GAD inventory CSVs, clean, and replace herd_inventory table."""
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    db.execute(delete(HerdInventory))

    rows_imported = 0
    for relative_path, farm in (
        (CM_INVENTORY_FILE, "CM"),
        (GAD_INVENTORY_FILE, "GAD"),
    ):
        rows_imported += _import_farm_file(db, relative_path, farm, import_time)

    db.commit()

    farm_counts = dict(
        db.execute(
            select(HerdInventory.farm, func.count()).group_by(HerdInventory.farm)
        ).all()
    )

    return {
        "rows_imported": rows_imported,
        "farm_counts": farm_counts,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": [CM_INVENTORY_FILE, GAD_INVENTORY_FILE],
    }
