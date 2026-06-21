"""Import birth records from DCEXPORT CMBORN / GADBORN CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import HerdBirth
from app.services.graph_onedrive import download_herd_file, graph_is_configured
from app.services.herd_import_utils import (
    bulk_insert_dataframe,
    birth_category_series,
    drop_unnamed_columns,
    fiscal_year_from_dates,
    parse_date_series,
    remove_invalid_id_rows,
)

CM_BIRTH_FILE = "DCEXPORTCM/CMBORN.CSV"
GAD_BIRTH_FILE = "DCEXPORTGAD/GADBORN.CSV"

_BIRTH_ENCODING = "windows-1252"
_BIRTH_REQUIRED_COLUMNS = ("ID", "ETAG", "BDAT", "CBRD", "GNDR")


def _clean_birth_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = drop_unnamed_columns(df)

    missing = [col for col in _BIRTH_REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in birth data: {missing}")

    str_cols = df.select_dtypes(include="object").columns.tolist()
    if str_cols:
        df[str_cols] = (
            df[str_cols]
            .astype(str)
            .apply(lambda col: col.str.replace(r"[\s\xa0\t\r\n]+", " ", regex=True))
            .apply(lambda col: col.str.strip())
        )

    df = df[list(_BIRTH_REQUIRED_COLUMNS) + ["Farm"]].copy()
    df = remove_invalid_id_rows(df)

    bdat_as_str = df["BDAT"].astype(str).str.strip()
    valid_bdat = bdat_as_str.str.contains(r"[/-]", regex=True, na=False)
    df = df[valid_bdat].copy()

    df["BDAT"] = parse_date_series(df["BDAT"])
    df["Fiscal Year"] = fiscal_year_from_dates(df["BDAT"])
    df["CBRD"] = pd.to_numeric(df["CBRD"], errors="coerce").astype("Int64")
    df["Category"] = birth_category_series(df["CBRD"], df["GNDR"])

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

    def series_int(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce").astype("Int64")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "cbrd": series_int("CBRD"),
            "gndr": series_str("GNDR"),
            "category": series_str("Category"),
            "farm": series_str("Farm"),
            "fiscal_year": series_int("Fiscal Year"),
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
        encoding=_BIRTH_ENCODING,
        dayfirst=True,
        on_bad_lines="skip",
    )
    del file_bytes

    df["Farm"] = farm
    df = _clean_birth_dataframe(df)
    bulk_insert_dataframe(db, HerdBirth, df, _dataframe_to_mappings, import_time)
    rows = len(df)
    del df
    gc.collect()
    return rows


def import_herd_births(db: Session) -> dict[str, Any]:
    """Download CM + GAD birth CSVs, clean, and replace herd_births table."""
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    db.execute(delete(HerdBirth))

    rows_imported = 0
    for relative_path, farm in (
        (CM_BIRTH_FILE, "CM"),
        (GAD_BIRTH_FILE, "GAD"),
    ):
        rows_imported += _import_farm_file(db, relative_path, farm, import_time)

    db.commit()

    farm_counts = dict(
        db.execute(select(HerdBirth.farm, func.count()).group_by(HerdBirth.farm)).all()
    )
    latest_birth = db.scalar(select(func.max(HerdBirth.bdat)))

    return {
        "rows_imported": rows_imported,
        "farm_counts": farm_counts,
        "latest_birth_date": latest_birth.isoformat() if latest_birth else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": [CM_BIRTH_FILE, GAD_BIRTH_FILE],
    }
