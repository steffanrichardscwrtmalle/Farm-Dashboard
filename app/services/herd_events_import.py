"""Import cow events from DCEXPORT CMEVENTS / GADEVENTS CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import CowEvent
from app.services.graph_onedrive import download_herd_file, graph_is_configured

CM_EVENTS_FILE = "DCEXPORTCM/CMEVENTS.CSV"
GAD_EVENTS_FILE = "DCEXPORTGAD/GADEVENTS.CSV"

_BATCH_SIZE = 2000
_CSV_CHUNK_SIZE = 25_000


def _clean_events_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

    for col in ["BDAT", "FDAT", "EDAT", "Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], dayfirst=True, errors="coerce")

    if "CBRD" in df.columns:
        df["CBRD"] = pd.to_numeric(df["CBRD"], errors="coerce").fillna(0).astype("Int64")

    if "GNDR" in df.columns:
        df["GNDR"] = df["GNDR"].replace(["", None], "F")

    if "Date" in df.columns:
        df["mmm-yy"] = df["Date"].dt.strftime("%b-%y").where(df["Date"].notna(), "")
        month = df["Date"].dt.month
        year = df["Date"].dt.year
        df["Fiscal Year"] = year.where(month < 4, year + 1).astype("Int64")
        adjusted_month = (month - 4).where(month >= 4, month + 9)
        valid = df["Date"].notna() & df["Fiscal Year"].notna()
        df["Sort Key"] = (df["Fiscal Year"] * 100 + adjusted_month).astype("Int64")
        df.loc[~valid, "Sort Key"] = pd.NA
    else:
        df["mmm-yy"] = ""
        df["Fiscal Year"] = pd.Series([pd.NA] * len(df), dtype="Int64")
        df["Sort Key"] = pd.Series([pd.NA] * len(df), dtype="Int64")

    if "LACT" in df.columns:
        df["Parity"] = None
        df.loc[df["LACT"] == 0, "Parity"] = "Primiparous"
        df.loc[df["LACT"].notna() & (df["LACT"] != 0), "Parity"] = "Multiparous"
    else:
        df["Parity"] = None

    event = df["Event"] if "Event" in df.columns else pd.Series([""] * len(df), index=df.index)
    remark = df["Remark"] if "Remark" in df.columns else pd.Series([""] * len(df), index=df.index)
    sold_mask = (event == "DIED") & remark.isin(["TB", "OFS"])
    df["Event"] = event.where(~sold_mask, "SOLD")

    if {"FDAT", "Date", "LACT"}.issubset(df.columns):
        fresh_mask = (
            df["Event"].str.upper().eq("ABORT")
            & df["FDAT"].notna()
            & df["Date"].notna()
            & (df["FDAT"] == df["Date"])
            & (df["LACT"] == 1)
        )
        df.loc[fresh_mask, "Event"] = "FRESH"

    return df


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    """Convert cleaned dataframe to dicts for bulk_insert_mappings."""

    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_date(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_datetime(df[col], errors="coerce").dt.date.replace({pd.NaT: None})

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
            "fdat": series_date("FDAT"),
            "lact": series_int("LACT"),
            "gndr": series_str("GNDR"),
            "edat": series_date("EDAT"),
            "event": series_str("Event"),
            "dim": series_float("DIM"),
            "event_date": series_date("Date"),
            "remark": series_str("Remark"),
            "r": series_str("R"),
            "t": series_str("T"),
            "b": series_str("B"),
            "protocols": series_str("Protocols"),
            "technician": series_str("Technician"),
            "farm": series_str("Farm"),
            "month_label": series_str("mmm-yy"),
            "fiscal_year": series_int("Fiscal Year"),
            "sort_key": series_int("Sort Key"),
            "parity": series_str("Parity"),
            "cbrd": series_int("CBRD"),
            "import_timestamp": import_time,
        }
    )
    records = out.to_dict(orient="records")
    for row in records:
        for key, val in list(row.items()):
            if pd.isna(val):
                row[key] = None
            elif hasattr(val, "item"):
                try:
                    row[key] = val.item()
                except (ValueError, AttributeError):
                    pass
    return records


def _insert_dataframe_in_batches(
    db: Session, df: pd.DataFrame, import_time: dt.datetime
) -> None:
    """Insert rows without building one giant mappings list in memory."""
    for start in range(0, len(df), _BATCH_SIZE):
        batch = df.iloc[start : start + _BATCH_SIZE]
        mappings = _dataframe_to_mappings(batch, import_time)
        db.bulk_insert_mappings(CowEvent, mappings)


def _import_farm_file(
    db: Session, relative_path: str, farm: str, import_time: dt.datetime
) -> int:
    """Download, clean, and insert one farm CSV in streaming chunks."""
    file_bytes = download_herd_file(relative_path)
    buffer = io.BytesIO(file_bytes)
    del file_bytes

    rows_imported = 0
    for chunk in pd.read_csv(
        buffer,
        encoding="utf-8",
        dayfirst=True,
        on_bad_lines="skip",
        chunksize=_CSV_CHUNK_SIZE,
    ):
        chunk["Farm"] = farm
        chunk = _clean_events_dataframe(chunk)
        _insert_dataframe_in_batches(db, chunk, import_time)
        rows_imported += len(chunk)
        del chunk

    del buffer
    gc.collect()
    return rows_imported


def import_cow_events(db: Session) -> dict[str, Any]:
    """Download CM + GAD event CSVs, clean, and replace cow_events table."""
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    db.execute(delete(CowEvent))

    rows_imported = 0
    for relative_path, farm in (
        (CM_EVENTS_FILE, "CM"),
        (GAD_EVENTS_FILE, "GAD"),
    ):
        rows_imported += _import_farm_file(db, relative_path, farm, import_time)

    db.commit()

    farm_counts = dict(
        db.execute(
            select(CowEvent.farm, func.count()).group_by(CowEvent.farm)
        ).all()
    )

    latest_date = db.scalar(select(func.max(CowEvent.event_date)))

    return {
        "rows_imported": rows_imported,
        "farm_counts": farm_counts,
        "latest_event_date": latest_date.isoformat() if latest_date else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": [CM_EVENTS_FILE, GAD_EVENTS_FILE],
    }
