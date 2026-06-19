"""Import cow events from DCEXPORT CMEVENTS / GADEVENTS CSV files."""

from __future__ import annotations

import datetime as dt
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


def _parse_events_csv(file_bytes: bytes, farm: str) -> pd.DataFrame:
    df = pd.read_csv(
        io.BytesIO(file_bytes),
        encoding="utf-8",
        dayfirst=True,
        on_bad_lines="skip",
    )
    df["Farm"] = farm
    return df


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
    else:
        df["mmm-yy"] = ""

    df["Fiscal Year"] = df["Date"].apply(
        lambda x: x.year + 1 if pd.notnull(x) and x.month >= 4 else (x.year if pd.notnull(x) else None)
    ).astype("Int64")

    def sort_key(row: pd.Series) -> int | None:
        if pd.isna(row["Date"]) or pd.isna(row["Fiscal Year"]):
            return None
        m = row["Date"].month
        adjusted = m - 4 if m >= 4 else m + 9
        return int(row["Fiscal Year"]) * 100 + adjusted

    df["Sort Key"] = df.apply(sort_key, axis=1).astype("Int64")

    if "LACT" in df.columns:
        df["Parity"] = df["LACT"].apply(
            lambda x: "Primiparous" if x == 0 else ("Multiparous" if pd.notnull(x) else None)
        )
    else:
        df["Parity"] = None

    def update_event(row: pd.Series) -> Any:
        event = row.get("Event", "")
        remark = row.get("Remark", "")
        fdat = row.get("FDAT")
        date_val = row.get("Date")
        lact = row.get("LACT", 0)

        if event == "DIED" and remark in ["TB", "OFS"]:
            return "SOLD"
        if (
            str(event).upper() == "ABORT"
            and pd.notnull(fdat)
            and pd.notnull(date_val)
            and fdat == date_val
            and lact == 1
        ):
            return "FRESH"
        return event

    df["Event"] = df.apply(update_event, axis=1)
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


def import_cow_events(db: Session) -> dict[str, Any]:
    """Download CM + GAD event CSVs, clean, and replace cow_events table."""
    if not graph_is_configured():
        raise ValueError(
            "Herd import is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR."
        )

    cm_bytes = download_herd_file(CM_EVENTS_FILE)
    gad_bytes = download_herd_file(GAD_EVENTS_FILE)

    df_cm = _parse_events_csv(cm_bytes, "CM")
    df_gad = _parse_events_csv(gad_bytes, "GAD")
    df = pd.concat([df_cm, df_gad], ignore_index=True)
    df = _clean_events_dataframe(df)

    import_time = dt.datetime.now()
    db.execute(delete(CowEvent))

    mappings = _dataframe_to_mappings(df, import_time)
    for i in range(0, len(mappings), _BATCH_SIZE):
        db.bulk_insert_mappings(CowEvent, mappings[i : i + _BATCH_SIZE])

    db.commit()

    farm_counts = dict(
        db.execute(
            select(CowEvent.farm, func.count()).group_by(CowEvent.farm)
        ).all()
    )

    latest_date = db.scalar(select(func.max(CowEvent.event_date)))

    return {
        "rows_imported": len(df),
        "farm_counts": farm_counts,
        "latest_event_date": latest_date.isoformat() if latest_date else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": [CM_EVENTS_FILE, GAD_EVENTS_FILE],
    }
