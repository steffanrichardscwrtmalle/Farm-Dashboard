"""Parse DelPro / milk-flow shift reports (OLE .xls emailed as .csv)."""

from __future__ import annotations

import datetime as dt
import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pandas as pd

REQUIRED_COLUMNS = (
    "ID",
    "Date",
    "Shift",
    "Yield",
    "Pen",
    "Duration",
    "Cow Milking Start Time",
)

OPTIONAL_COLUMNS = (
    "Average Flow",
    "DIM",
    "Peak Flow",
    "Time To Peak",
    "15s Flow",
    "30s Flow",
    "60s Flow",
    "120s Flow",
    "% 2 minutes",
    "Milk Yield at 2 Minutes",
    "Flow Rate at Removal",
    "Identified At Milking",
    "Final Detaching",
    "Extra Attachments",
    "Milking Point",
)


@dataclass
class ParsedMilkFlowRow:
    cow_id: str
    milking_date: dt.date
    shift: str
    pen: int | None
    milking_point: int | None
    dim: int | None
    yield_kg: float | None
    average_flow: float | None
    peak_flow: float | None
    time_to_peak_seconds: int | None
    flow_15s: float | None
    flow_30s: float | None
    flow_60s: float | None
    flow_120s: float | None
    pct_2_minutes: float | None
    milk_yield_2_minutes: float | None
    flow_rate_at_removal: float | None
    duration_seconds: int | None
    start_seconds: int | None
    identified_at_milking: str | None
    final_detaching: str | None
    extra_attachments: str | None


@dataclass
class ParsedMilkFlowReport:
    farm: str
    milking_date: dt.date
    shift: str
    rows: list[ParsedMilkFlowRow]
    source_filename: str


def detect_farm_from_filename(filename: str) -> str | None:
    name = (filename or "").upper()
    if re.search(r"\bGAD\b", name) or "GREEN ACRE" in name or "GREENACRE" in name:
        return "GAD"
    if re.search(r"\bCM\b", name) or "CWRT" in name:
        return "CM"
    return None


def is_milk_flow_report_filename(filename: str) -> bool:
    """True for Dataflow 'Milk Flow Report Export CM/GAD' attachments."""
    name = (filename or "").strip().lower()
    if "milk flow report" not in name:
        return False
    return detect_farm_from_filename(filename) in {"CM", "GAD"}


def _to_seconds(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, dt.datetime):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, (int, float)) and not pd.isna(value):
        # Excel serial time fraction of a day
        if 0 <= float(value) < 1.5:
            return int(round(float(value) * 86400)) % 86400
        return int(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    for fmt in ("%H:%M:%S", "%H:%M", "%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, fmt).time()
            if fmt == "%M:%S":
                return parsed.minute * 60 + parsed.second
            return parsed.hour * 3600 + parsed.minute * 60 + parsed.second
        except ValueError:
            continue
    return None


def _to_date(value: Any) -> dt.date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(ts):
        return None
    return ts.date()


def _to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    if f is None:
        return None
    return int(round(f))


def _to_str(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _to_cow_id(value: Any) -> str | None:
    """Normalise DelPro animal IDs (often floats like 515548.0 in Excel)."""
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


def read_milk_flow_dataframe(content: bytes) -> pd.DataFrame:
    """Load report bytes; Dataflow files are often OLE .xls even when named .csv."""
    if not content:
        raise ValueError("Milk flow report is empty")

    is_ole = content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    is_zip = content[:2] == b"PK"

    def _drop_unnamed(df: pd.DataFrame) -> pd.DataFrame:
        drop_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
        return df.drop(columns=drop_cols) if drop_cols else df

    if is_ole:
        try:
            import xlrd  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "Cannot read Dataflow milk-flow report (Excel .xls named .csv). "
                "Install xlrd: pip install xlrd"
            ) from exc
        try:
            return _drop_unnamed(
                pd.read_excel(io.BytesIO(content), engine="xlrd")
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read OLE milk-flow workbook with xlrd: {exc}"
            ) from exc

    if is_zip:
        try:
            return _drop_unnamed(
                pd.read_excel(io.BytesIO(content), engine="openpyxl")
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read .xlsx milk-flow workbook: {exc}"
            ) from exc

    # Plain-text CSV (or mislabelled binary — try Excel engines before failing).
    try:
        return _drop_unnamed(pd.read_csv(io.BytesIO(content)))
    except UnicodeDecodeError:
        try:
            import xlrd  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "Attachment looks like Excel .xls but xlrd is not installed "
                "(pip install xlrd)."
            ) from exc
        try:
            return _drop_unnamed(
                pd.read_excel(io.BytesIO(content), engine="xlrd")
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read milk-flow report as CSV or Excel: {exc}"
            ) from exc
    except Exception as exc:  # noqa: BLE001
        # Last resort: try Excel engines for odd text/binary hybrids.
        for engine in ("xlrd", "openpyxl"):
            try:
                return _drop_unnamed(
                    pd.read_excel(io.BytesIO(content), engine=engine)
                )
            except Exception:
                continue
        raise ValueError(f"Could not read milk-flow report: {exc}") from exc


def parse_milk_flow_report(
    content: bytes,
    *,
    filename: str = "",
    farm: str | None = None,
) -> list[ParsedMilkFlowReport]:
    """Parse a milk-flow file into one report per (date, shift).

    Combined exports with multiple shifts (or days) are split automatically.
    """
    df = read_milk_flow_dataframe(content)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Milk flow report is missing columns: {', '.join(missing)}"
        )

    resolved_farm = (farm or detect_farm_from_filename(filename) or "").upper()
    if resolved_farm not in {"CM", "GAD"}:
        raise ValueError(
            "Could not determine farm (CM/GAD). Include CM or GAD in the "
            "filename, or pass farm explicitly."
        )

    rows: list[ParsedMilkFlowRow] = []
    for _, raw in df.iterrows():
        shift = _to_str(raw.get("Shift"))
        milking_date = _to_date(raw.get("Date"))
        cow_id = _to_cow_id(raw.get("ID"))
        start_seconds = _to_seconds(raw.get("Cow Milking Start Time"))
        # Skip trailing summary/total rows (no shift, date, or milking start).
        if not shift or not milking_date or not cow_id or start_seconds is None:
            continue
        # Dataflow labels Morning shifts with the previous calendar date.
        if shift.strip().casefold() == "morning":
            milking_date = milking_date + dt.timedelta(days=1)
        rows.append(
            ParsedMilkFlowRow(
                cow_id=cow_id,
                milking_date=milking_date,
                shift=shift,
                pen=_to_int(raw.get("Pen")),
                milking_point=_to_int(raw.get("Milking Point")),
                dim=_to_int(raw.get("DIM")),
                yield_kg=_to_float(raw.get("Yield")),
                average_flow=_to_float(raw.get("Average Flow")),
                peak_flow=_to_float(raw.get("Peak Flow")),
                time_to_peak_seconds=_to_seconds(raw.get("Time To Peak")),
                flow_15s=_to_float(raw.get("15s Flow")),
                flow_30s=_to_float(raw.get("30s Flow")),
                flow_60s=_to_float(raw.get("60s Flow")),
                flow_120s=_to_float(raw.get("120s Flow")),
                pct_2_minutes=_to_float(raw.get("% 2 minutes")),
                milk_yield_2_minutes=_to_float(raw.get("Milk Yield at 2 Minutes")),
                flow_rate_at_removal=_to_float(raw.get("Flow Rate at Removal")),
                duration_seconds=_to_seconds(raw.get("Duration")),
                start_seconds=start_seconds,
                identified_at_milking=_to_str(raw.get("Identified At Milking")),
                final_detaching=_to_str(raw.get("Final Detaching")),
                extra_attachments=_to_str(raw.get("Extra Attachments")),
            )
        )

    if not rows:
        raise ValueError("No valid cow milking rows found in the report.")

    by_shift: dict[tuple[dt.date, str], list[ParsedMilkFlowRow]] = {}
    for row in rows:
        by_shift.setdefault((row.milking_date, row.shift), []).append(row)

    reports = [
        ParsedMilkFlowReport(
            farm=resolved_farm,
            milking_date=milking_date,
            shift=shift,
            rows=shift_rows,
            source_filename=filename or "",
        )
        for (milking_date, shift), shift_rows in sorted(by_shift.items())
    ]
    return reports



def shift_timeline_origin(start_seconds_list: list[int]) -> int | None:
    """Return the clock-second that marks the start of a shift (after midnight wrap)."""
    starts = [s for s in start_seconds_list if s is not None]
    if not starts:
        return None
    ordered = sorted(set(starts))
    if len(ordered) == 1:
        return ordered[0]
    gaps: list[tuple[int, int]] = []
    for i in range(len(ordered) - 1):
        gaps.append((ordered[i + 1] - ordered[i], i))
    wrap_gap = (ordered[0] + 86400) - ordered[-1]
    gaps.append((wrap_gap, len(ordered) - 1))
    gaps.sort(reverse=True)
    boundary_idx = gaps[0][1]
    return ordered[(boundary_idx + 1) % len(ordered)]


def to_absolute_start(start_seconds: int, origin: int) -> int:
    """Map a clock time onto a monotonic shift timeline (origin may be late evening)."""
    if start_seconds < origin:
        return start_seconds + 86400
    return start_seconds


def milking_span_seconds(
    start_duration_pairs: list[tuple[int, int | None]],
) -> tuple[int | None, int | None, int | None]:
    """Return (first_start, last_end, span_seconds) handling midnight wrap.

    Shift end = last cow's start time + that cow's milking duration.
    When starts wrap past midnight, late-evening times are treated as before dawn.
    """
    usable = [(s, d or 0) for s, d in start_duration_pairs if s is not None]
    if not usable:
        return None, None, None

    origin = shift_timeline_origin([s for s, _ in usable])
    if origin is None:
        return None, None, None

    abs_pairs = [(to_absolute_start(s, origin), d) for s, d in usable]
    first_abs = min(p[0] for p in abs_pairs)
    last_start_abs, last_dur = max(abs_pairs, key=lambda p: p[0])
    last_end_abs = last_start_abs + last_dur
    span = last_end_abs - first_abs
    return first_abs % 86400, last_end_abs, span


# Pen corrections: a cow milked among another pen's cohort is treated as that pen.
PEN_COHORT_WINDOW_SECONDS = 10 * 60
PEN_COHORT_MIN_NEIGHBOURS = 8
PEN_COHORT_MIN_SHARE = 0.60
# Split a pen into separate milking sessions when cows are this far apart.
PEN_SESSION_GAP_SECONDS = 45 * 60


def correct_pens_by_milking_cohort(
    pens: list[int | None],
    abs_starts: list[int],
    *,
    window_seconds: int = PEN_COHORT_WINDOW_SECONDS,
    min_neighbours: int = PEN_COHORT_MIN_NEIGHBOURS,
    min_share: float = PEN_COHORT_MIN_SHARE,
) -> tuple[list[int | None], int]:
    """Reassign stray cows to the pen they were milked among.

    For each cow, look at milking starts within ±window. If another pen has a clear
    majority in that local cohort, treat the cow as that pen (unrecorded movements).
    """
    n = len(pens)
    if n == 0 or len(abs_starts) != n:
        return list(pens), 0

    order = sorted(range(n), key=lambda i: abs_starts[i])
    corrected = list(pens)
    changes = 0

    for rank, i in enumerate(order):
        recorded = pens[i]
        if recorded is None:
            continue
        t = abs_starts[i]
        local: list[int] = []
        j = rank
        while j >= 0 and abs_starts[order[j]] >= t - window_seconds:
            p = pens[order[j]]
            if p is not None:
                local.append(p)
            j -= 1
        j = rank + 1
        while j < n and abs_starts[order[j]] <= t + window_seconds:
            p = pens[order[j]]
            if p is not None:
                local.append(p)
            j += 1
        if len(local) < min_neighbours:
            continue
        majority, count = Counter(local).most_common(1)[0]
        if majority != recorded and (count / len(local)) >= min_share:
            corrected[i] = majority
            changes += 1

    return corrected, changes


def pen_session_span_seconds(
    abs_start_duration_pairs: list[tuple[int, int]],
    *,
    gap_seconds: int = PEN_SESSION_GAP_SECONDS,
) -> tuple[int | None, int | None, int | None, int]:
    """Span for a pen, splitting on large gaps so early/late outliers don't inflate duration.

    Returns (first_start_clock, last_end_abs, total_span_seconds, session_count).
    total_span_seconds is the sum of each contiguous session's duration.
    """
    if not abs_start_duration_pairs:
        return None, None, None, 0

    ordered = sorted(abs_start_duration_pairs, key=lambda p: p[0])
    sessions: list[list[tuple[int, int]]] = [[ordered[0]]]
    for pair in ordered[1:]:
        if pair[0] - sessions[-1][-1][0] > gap_seconds:
            sessions.append([pair])
        else:
            sessions[-1].append(pair)

    total_span = 0
    first_abs = ordered[0][0]
    last_end_abs = ordered[0][0]
    for session in sessions:
        sess_first = session[0][0]
        last_start, last_dur = max(session, key=lambda p: p[0])
        sess_end = last_start + last_dur
        total_span += sess_end - sess_first
        last_end_abs = max(last_end_abs, sess_end)

    return first_abs % 86400, last_end_abs, total_span, len(sessions)
