"""Parse Dataflow Rotary Entry ID reports (OLE .xls emailed as .csv)."""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.parlour_milk_flow_parse import detect_farm_from_filename

REQUIRED_COLUMNS = (
    "Cow Number",
    "Identification Time",
)

# Date is the calendar day of the ID pass. Identification Time carries the clock
# time, but Dataflow often stamps its date as the export day — so we combine
# Date + time-of-day from Identification Time when Date is present.
OPTIONAL_COLUMNS = ("Date",)


@dataclass
class ParsedRotaryEntryIdEvent:
    cow_id: str
    identified_at: dt.datetime
    id_seconds: int


@dataclass
class ParsedRotaryEntryIdReport:
    farm: str
    events: list[ParsedRotaryEntryIdEvent]
    source_filename: str


def is_rotary_entry_id_filename(filename: str) -> bool:
    """True for Dataflow 'Rotary Entry ID CM/GAD' attachments."""
    name = (filename or "").strip().lower()
    if "rotary entry id" not in name:
        return False
    return detect_farm_from_filename(filename) in {"CM", "GAD"}


def _to_cow_id(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value).rstrip("0").rstrip(".") or None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    try:
        as_float = float(text)
        if as_float.is_integer():
            return str(int(as_float))
    except ValueError:
        pass
    return text


def _to_datetime(value: Any) -> dt.datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        py = value.to_pydatetime()
        if getattr(py, "tzinfo", None) is not None:
            py = py.replace(tzinfo=None)
        return py
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return dt.datetime.combine(value, dt.time.min)
    ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(ts):
        return None
    py = ts.to_pydatetime()
    if getattr(py, "tzinfo", None) is not None:
        py = py.replace(tzinfo=None)
    return py


def _to_date(value: Any) -> dt.date | None:
    parsed = _to_datetime(value)
    if parsed is None:
        return None
    return parsed.date()


def _identified_at_from_row(row: Any) -> dt.datetime | None:
    """Build ID timestamp: Date (calendar) + Identification Time (clock).

    Dataflow cumulative exports often put the correct day in Date while
    Identification Time keeps the real clock but a wrong export-day date.
    """
    id_dt = _to_datetime(row.get("Identification Time"))
    if id_dt is None or pd.isna(id_dt) or not isinstance(id_dt, dt.datetime):
        return None
    clock = dt.time(
        id_dt.hour,
        id_dt.minute,
        id_dt.second,
        id_dt.microsecond,
    )
    calendar_day = _to_date(row.get("Date"))
    if calendar_day is not None:
        return dt.datetime.combine(calendar_day, clock)
    return dt.datetime(
        id_dt.year,
        id_dt.month,
        id_dt.day,
        id_dt.hour,
        id_dt.minute,
        id_dt.second,
        id_dt.microsecond,
    )


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename: dict[Any, str] = {}
    for col in df.columns:
        text = str(col).strip()
        key = text.casefold()
        if key in {"cow number", "cow", "id", "animal id", "animal number"}:
            rename[col] = "Cow Number"
        elif key in {"identification time", "identified at", "id time"}:
            rename[col] = "Identification Time"
        elif key == "date":
            rename[col] = "Date"
    if rename:
        df = df.rename(columns=rename)
    return df


def read_rotary_entry_id_dataframe(content: bytes) -> pd.DataFrame:
    """Load report bytes; Dataflow files are often OLE .xls even when named .csv."""
    if not content:
        raise ValueError("Rotary Entry ID report is empty")

    is_ole = content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    is_zip = content[:2] == b"PK"

    def _drop_unnamed(df: pd.DataFrame) -> pd.DataFrame:
        drop_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
        cleaned = df.drop(columns=drop_cols) if drop_cols else df
        return _normalize_columns(cleaned)

    if is_ole:
        try:
            import xlrd  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "Cannot read Dataflow Rotary Entry ID report (Excel .xls named .csv). "
                "Install xlrd: pip install xlrd"
            ) from exc
        try:
            return _drop_unnamed(pd.read_excel(io.BytesIO(content), engine="xlrd"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read OLE Rotary Entry ID workbook with xlrd: {exc}"
            ) from exc

    if is_zip:
        try:
            return _drop_unnamed(pd.read_excel(io.BytesIO(content), engine="openpyxl"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read .xlsx Rotary Entry ID workbook: {exc}"
            ) from exc

    try:
        return _drop_unnamed(
            pd.read_csv(io.BytesIO(content), encoding="utf-8", low_memory=False)
        )
    except UnicodeDecodeError:
        return _drop_unnamed(
            pd.read_csv(io.BytesIO(content), encoding="latin-1", low_memory=False)
        )


def parse_rotary_entry_id_report(
    content: bytes,
    *,
    filename: str = "",
    farm: str | None = None,
) -> ParsedRotaryEntryIdReport:
    farm_key = (farm or detect_farm_from_filename(filename) or "").upper()
    if farm_key not in {"CM", "GAD"}:
        raise ValueError(
            "Could not detect farm from Rotary Entry ID filename "
            f"(expected CM or GAD): {filename!r}"
        )

    df = read_rotary_entry_id_dataframe(content)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Rotary Entry ID report missing columns: "
            + ", ".join(missing)
            + f" (got: {list(df.columns)})"
        )

    events: list[ParsedRotaryEntryIdEvent] = []
    seen: set[tuple[str, dt.datetime]] = set()
    for _, row in df.iterrows():
        cow_id = _to_cow_id(row.get("Cow Number"))
        identified_at = _identified_at_from_row(row)
        if (
            not cow_id
            or cow_id.lower() in {"nat", "nan", "none"}
            or identified_at is None
        ):
            continue
        key = (cow_id, identified_at)
        if key in seen:
            continue
        seen.add(key)
        id_seconds = (
            identified_at.hour * 3600
            + identified_at.minute * 60
            + identified_at.second
        )
        events.append(
            ParsedRotaryEntryIdEvent(
                cow_id=cow_id,
                identified_at=identified_at,
                id_seconds=id_seconds,
            )
        )

    if not events:
        raise ValueError("Rotary Entry ID report has no usable identification rows")

    return ParsedRotaryEntryIdReport(
        farm=farm_key,
        events=events,
        source_filename=filename or "",
    )
