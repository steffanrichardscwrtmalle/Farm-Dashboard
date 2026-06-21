"""Shared helpers for DCEXPORT herd CSV imports."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

HERD_DATE_FORMAT = "%d/%m/%y"
BATCH_SIZE = 2000
BEEF_CBREED_MIN = 102
CATEGORY_DAIRY = "Dairy"
CATEGORY_BEEF = "Beef"


def category_from_birth(cbrd: int | float | None, gndr: str | None) -> str:
    """Dairy when CBRD < 102 and GNDR is F; all other rows are beef."""
    try:
        if cbrd is None or pd.isna(cbrd):
            return CATEGORY_BEEF
        code = int(cbrd)
    except (TypeError, ValueError):
        return CATEGORY_BEEF

    gender = str(gndr).strip().upper() if gndr is not None and not pd.isna(gndr) else ""
    if code < BEEF_CBREED_MIN and gender == "F":
        return CATEGORY_DAIRY
    return CATEGORY_BEEF


def birth_category_series(cbrd: pd.Series, gndr: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(cbrd, errors="coerce")
    gender = gndr.astype("string").str.strip().str.upper()
    dairy_mask = numeric.notna() & (numeric < BEEF_CBREED_MIN) & (gender == "F")
    out = pd.Series(CATEGORY_BEEF, index=cbrd.index, dtype="object")
    out.loc[dairy_mask] = CATEGORY_DAIRY
    return out


def parse_date_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    return pd.to_datetime(series, format=HERD_DATE_FORMAT, errors="coerce")


def fiscal_year_from_dates(dates: pd.Series) -> pd.Series:
    month = dates.dt.month
    year = dates.dt.year
    return year.where(month < 4, year + 1).astype("Int64")


def strip_string_columns(df: pd.DataFrame) -> pd.DataFrame:
    str_cols = df.select_dtypes(include="object").columns
    if len(str_cols):
        df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())
    return df


def drop_unnamed_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]


def remove_invalid_id_rows(df: pd.DataFrame, id_column: str = "ID") -> pd.DataFrame:
    if id_column not in df.columns:
        return df
    ids = df[id_column].astype(str).str.strip()
    return df[
        ids.str.upper().ne("ID")
        & ids.ne("")
        & ids.str.lower().ne("nan")
    ].copy()


def normalize_mapping_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def bulk_insert_dataframe(
    db: Session,
    model: type,
    df: pd.DataFrame,
    mapping_fn: Callable[[pd.DataFrame, dt.datetime], list[dict[str, Any]]],
    import_time: dt.datetime,
) -> None:
    for start in range(0, len(df), BATCH_SIZE):
        batch = df.iloc[start : start + BATCH_SIZE]
        mappings = normalize_mapping_records(mapping_fn(batch, import_time))
        db.bulk_insert_mappings(model, mappings)
