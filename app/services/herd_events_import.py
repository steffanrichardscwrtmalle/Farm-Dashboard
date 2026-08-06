"""Import cow events from DCEXPORT CMEVENTS / GADEVENTS CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import CowEvent
from app.services.graph_onedrive import (
    download_herd_file,
    graph_is_configured,
    herd_file_meta,
)
from app.services.herd_import_utils import (
    FP_EVENTS,
    dedupe_exit_event_rows,
    dedupe_fresh_event_rows,
    load_source_fingerprint,
    source_fingerprint,
    store_source_fingerprint,
)
from app.services.stock_purchase_derivation import rebuild_stock_purchases

logger = logging.getLogger(__name__)

CM_EVENTS_FILE = "DCEXPORTCM/CMEVENTS.CSV"
GAD_EVENTS_FILE = "DCEXPORTGAD/GADEVENTS.CSV"
_EVENT_FILES = (
    (CM_EVENTS_FILE, "CM"),
    (GAD_EVENTS_FILE, "GAD"),
)

_BATCH_SIZE = 2000
_CSV_CHUNK_SIZE = 25_000
_EVENT_DATE_COLUMNS = ("BDAT", "FDAT", "EDAT", "Date")
_EVENT_DATE_FORMAT = "%d/%m/%y"


def _parse_date_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    return pd.to_datetime(series, format=_EVENT_DATE_FORMAT, errors="coerce")


def _clean_events_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

    for col in _EVENT_DATE_COLUMNS:
        if col in df.columns:
            df[col] = _parse_date_series(df[col])

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
    # Keep original DairyComp SOLD/DIED. Sales reporting treats DIED+TB/OFS as
    # sales via sales_classified_event_clause(); BCMS needs the true DIED type.

    if {"FDAT", "Date", "LACT"}.issubset(df.columns):
        fresh_mask = (
            event.str.upper().eq("ABORT")
            & df["FDAT"].notna()
            & df["Date"].notna()
            & (df["FDAT"] == df["Date"])
            & (df["LACT"] == 1)
        )
        df.loc[fresh_mask, "Event"] = "FRESH"

    if {"Date", "EDAT"}.issubset(df.columns):
        invalid_edat = df["Date"].notna() & df["EDAT"].notna() & (df["Date"] < df["EDAT"])
        df = df.loc[~invalid_edat]

    df, _ = dedupe_fresh_event_rows(df)
    df, _ = dedupe_exit_event_rows(df)
    return df


def _remove_duplicate_cow_events(db: Session, event: str) -> int:
    """Delete duplicate rows for one event type; keep the lowest id per animal/date/lact."""
    animal_key = func.coalesce(CowEvent.etag, CowEvent.cow_id)
    keep_ids = (
        select(func.min(CowEvent.id))
        .where(CowEvent.event == event)
        .where(CowEvent.event_date.isnot(None))
        .group_by(CowEvent.farm, animal_key, CowEvent.event_date, CowEvent.lact)
    )
    result = db.execute(
        delete(CowEvent).where(
            CowEvent.event == event,
            ~CowEvent.id.in_(keep_ids),
        )
    )
    return int(result.rowcount or 0)


def remove_duplicate_fresh_cow_events(db: Session) -> int:
    """Delete duplicate FRESH rows in cow_events; keep the lowest id per animal/date/lact."""
    return _remove_duplicate_cow_events(db, "FRESH")


def remove_duplicate_exit_cow_events(db: Session) -> int:
    """Delete duplicate SOLD/DIED rows in cow_events."""
    return _remove_duplicate_cow_events(db, "SOLD") + _remove_duplicate_cow_events(
        db, "DIED"
    )


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
        return _parse_date_series(df[col]).dt.date.replace({pd.NaT: None})

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
            "dest": series_str("DEST"),
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
        parse_dates=list(_EVENT_DATE_COLUMNS),
        date_format=_EVENT_DATE_FORMAT,
    ):
        chunk["Farm"] = farm
        chunk = _clean_events_dataframe(chunk)
        _insert_dataframe_in_batches(db, chunk, import_time)
        rows_imported += len(chunk)
        del chunk

    del buffer
    gc.collect()
    return rows_imported


def import_cow_events(db: Session, *, force: bool = True) -> dict[str, Any]:
    """Download CM / GAD event CSVs and replace those farms' cow_events rows.

    When ``force=False``, each farm is checked independently against its stored
    OneDrive last-modified fingerprint. Unchanged farms are left alone.
    """
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    sources: list[dict[str, str]] = []
    farms_imported: list[str] = []
    farms_skipped: list[str] = []
    rows_imported = 0

    for relative_path, farm in _EVENT_FILES:
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
            stored = load_source_fingerprint(db, FP_EVENTS, farm)
            if stored == fingerprint:
                farms_skipped.append(farm)
                logger.info("Herd events %s unchanged; skipping", farm)
                continue

        db.execute(delete(CowEvent).where(CowEvent.farm == farm))
        rows_imported += _import_farm_file(db, relative_path, farm, import_time)
        store_source_fingerprint(db, FP_EVENTS, farm, fingerprint)
        farms_imported.append(farm)

    if farms_imported:
        duplicate_fresh_dropped = remove_duplicate_fresh_cow_events(db)
        duplicate_exit_dropped = remove_duplicate_exit_cow_events(db)
        purchase_stats = rebuild_stock_purchases(db)
        db.commit()
    else:
        duplicate_fresh_dropped = 0
        duplicate_exit_dropped = 0
        purchase_stats = {}

    farm_counts = dict(
        db.execute(
            select(CowEvent.farm, func.count()).group_by(CowEvent.farm)
        ).all()
    )
    latest_date = db.scalar(select(func.max(CowEvent.event_date)))
    source_files = [item["source_file"] for item in sources]
    all_skipped = bool(farms_skipped) and not farms_imported

    if all_skipped:
        return {
            "skipped": True,
            "reason": "source_unchanged",
            "rows_imported": sum(int(v) for v in farm_counts.values()),
            "duplicate_fresh_dropped": 0,
            "duplicate_exit_dropped": 0,
            "farm_counts": farm_counts,
            "farms_imported": [],
            "farms_skipped": farms_skipped,
            "latest_event_date": latest_date.isoformat() if latest_date else None,
            "imported_at": None,
            "source_files": source_files,
            "sources": sources,
            "purchase_stats": {},
        }

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "duplicate_fresh_dropped": duplicate_fresh_dropped,
        "duplicate_exit_dropped": duplicate_exit_dropped,
        "farm_counts": farm_counts,
        "farms_imported": farms_imported,
        "farms_skipped": farms_skipped,
        "latest_event_date": latest_date.isoformat() if latest_date else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": source_files,
        "sources": sources,
        "purchase_stats": purchase_stats,
    }
