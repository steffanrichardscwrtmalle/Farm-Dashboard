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

# Farm-specific header variants → canonical column names used below.
# Casing-only differences (e.g. "% 2 Minutes") are handled by casefold match.
COLUMN_ALIASES: dict[str, str] = {
    "2 minute yield": "Milk Yield at 2 Minutes",
    "2 minutes yield": "Milk Yield at 2 Minutes",
}


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


def normalize_shift_name(shift: str) -> str:
    """Map farm-specific report shift labels onto app shift names.

    GAD reports use Afternoon (sometimes misspelled) for the Day shift.
    """
    key = (shift or "").strip().casefold()
    if key in {"afternoon", "afternnoon", "aftenoon"}:
        return "Day"
    # Preserve familiar casing for the common three.
    if key == "morning":
        return "Morning"
    if key == "day":
        return "Day"
    if key == "night":
        return "Night"
    if key == "evening":
        return "Evening"
    return (shift or "").strip()


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


def _normalize_milk_flow_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename farm-specific headers onto the canonical CM column names."""
    rename: dict[Any, str] = {}
    used_targets: set[str] = set()
    for col in df.columns:
        text = str(col).strip()
        key = text.casefold()
        canonical = COLUMN_ALIASES.get(key)
        if canonical is None:
            # Case-insensitive match to known required/optional headers.
            for known in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS):
                if key == known.casefold():
                    canonical = known
                    break
        if canonical is None or canonical == col:
            continue
        if canonical in used_targets or canonical in df.columns:
            # Prefer an already-canonical column; skip alias clash.
            continue
        rename[col] = canonical
        used_targets.add(canonical)
    if rename:
        df = df.rename(columns=rename)
    return df


def read_milk_flow_dataframe(content: bytes) -> pd.DataFrame:
    """Load report bytes; Dataflow files are often OLE .xls even when named .csv."""
    if not content:
        raise ValueError("Milk flow report is empty")

    is_ole = content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    is_zip = content[:2] == b"PK"

    def _drop_unnamed(df: pd.DataFrame) -> pd.DataFrame:
        drop_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
        cleaned = df.drop(columns=drop_cols) if drop_cols else df
        return _normalize_milk_flow_columns(cleaned)

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


MORNING_EVENING_START_SECONDS = 12 * 3600


def _morning_looks_overnight(start_seconds_list: list[int]) -> bool | None:
    """True if Morning rows span evening→dawn (previous-day Date stamp)."""
    if not start_seconds_list:
        return None
    evening = sum(1 for s in start_seconds_list if s >= MORNING_EVENING_START_SECONDS)
    morning = sum(1 for s in start_seconds_list if s < MORNING_EVENING_START_SECONDS)
    if evening > 0 and morning > 0:
        return True
    if morning > 0 and evening == 0:
        return False
    if evening > 0 and morning == 0:
        return True
    return None


def resolve_cm_morning_date(
    raw_date: dt.date,
    *,
    peer_non_morning_dates: set[dt.date],
    start_seconds_list: list[int] | None = None,
) -> dt.date:
    """Map CM Morning report Date onto the calendar milking day.

    Older Dataflow exports stamp Morning with the previous calendar date (so we
    add one day). Newer exports already use the milking day. Prefer aligning with
    Day/Night dates from the same file or already imported for that farm; when
    that is ambiguous, overnight start times imply a previous-day stamp.
    """
    bumped = raw_date + dt.timedelta(days=1)
    raw_peer = raw_date in peer_non_morning_dates
    bumped_peer = bumped in peer_non_morning_dates
    if raw_peer and not bumped_peer:
        return raw_date
    if bumped_peer and not raw_peer:
        return bumped

    overnight = _morning_looks_overnight(start_seconds_list or [])
    if overnight is True:
        return bumped
    if overnight is False:
        return raw_date

    # Both peer dates exist (e.g. previous Night + next Day): prefer the raw
    # date — current Dataflow stamps Morning on the milking day.
    if raw_peer:
        return raw_date
    # No peer / clock signal — legacy +1 for older previous-day stamps.
    return bumped


def parse_milk_flow_report(
    content: bytes,
    *,
    filename: str = "",
    farm: str | None = None,
    peer_non_morning_dates: set[dt.date] | None = None,
    fallback_date: dt.date | None = None,
) -> list[ParsedMilkFlowReport]:
    """Parse a milk-flow file into one report per (date, shift).

    Combined exports with multiple shifts (or days) are split automatically.

    ``fallback_date`` is used when the Date column is missing or blank (some
    Dataflow exports omit it). Prefer the email received calendar day in the
    farm timezone. Dates filled from the fallback skip the CM Morning +1 quirk,
    because received time is already on the milking day.
    """
    df = read_milk_flow_dataframe(content)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if "Date" in missing and fallback_date is not None:
        missing = [c for c in missing if c != "Date"]
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

    draft: list[dict[str, Any]] = []
    for _, raw in df.iterrows():
        raw_shift = _to_str(raw.get("Shift"))
        raw_date = _to_date(raw.get("Date")) if "Date" in df.columns else None
        date_from_fallback = False
        if raw_date is None and fallback_date is not None:
            raw_date = fallback_date
            date_from_fallback = True
        cow_id = _to_cow_id(raw.get("ID"))
        start_seconds = _to_seconds(raw.get("Cow Milking Start Time"))
        # Skip trailing summary/total rows (no shift, date, or milking start).
        if not raw_shift or not raw_date or not cow_id or start_seconds is None:
            continue
        shift = normalize_shift_name(raw_shift)
        draft.append(
            {
                "cow_id": cow_id,
                "raw_date": raw_date,
                "date_from_fallback": date_from_fallback,
                "shift": shift,
                "pen": _to_int(raw.get("Pen")),
                "milking_point": _to_int(raw.get("Milking Point")),
                "dim": _to_int(raw.get("DIM")),
                "yield_kg": _to_float(raw.get("Yield")),
                "average_flow": _to_float(raw.get("Average Flow")),
                "peak_flow": _to_float(raw.get("Peak Flow")),
                "time_to_peak_seconds": _to_seconds(raw.get("Time To Peak")),
                "flow_15s": _to_float(raw.get("15s Flow")),
                "flow_30s": _to_float(raw.get("30s Flow")),
                "flow_60s": _to_float(raw.get("60s Flow")),
                "flow_120s": _to_float(raw.get("120s Flow")),
                "pct_2_minutes": _to_float(raw.get("% 2 minutes")),
                "milk_yield_2_minutes": _to_float(raw.get("Milk Yield at 2 Minutes")),
                "flow_rate_at_removal": _to_float(raw.get("Flow Rate at Removal")),
                "duration_seconds": _to_seconds(raw.get("Duration")),
                "start_seconds": start_seconds,
                "identified_at_milking": _to_str(raw.get("Identified At Milking")),
                "final_detaching": _to_str(raw.get("Final Detaching")),
                "extra_attachments": _to_str(raw.get("Extra Attachments")),
            }
        )

    if not draft:
        raise ValueError("No valid cow milking rows found in the report.")

    file_peers = {
        item["raw_date"]
        for item in draft
        if str(item["shift"]).casefold() != "morning"
    }
    peers = set(peer_non_morning_dates or ()) | file_peers
    morning_starts_by_date: dict[dt.date, list[int]] = {}
    for item in draft:
        if str(item["shift"]).casefold() != "morning":
            continue
        morning_starts_by_date.setdefault(item["raw_date"], []).append(
            int(item["start_seconds"])
        )

    rows: list[ParsedMilkFlowRow] = []
    for item in draft:
        milking_date = item["raw_date"]
        # CM Dataflow sometimes labels Morning with the previous calendar date.
        # GAD Morning dates are already correct — do not bump.
        # Skip bump when Date was filled from email received time.
        if (
            resolved_farm == "CM"
            and str(item["shift"]).casefold() == "morning"
            and not item.get("date_from_fallback")
        ):
            milking_date = resolve_cm_morning_date(
                item["raw_date"],
                peer_non_morning_dates=peers,
                start_seconds_list=morning_starts_by_date.get(item["raw_date"]),
            )
        rows.append(
            ParsedMilkFlowRow(
                cow_id=item["cow_id"],
                milking_date=milking_date,
                shift=item["shift"],
                pen=item["pen"],
                milking_point=item["milking_point"],
                dim=item["dim"],
                yield_kg=item["yield_kg"],
                average_flow=item["average_flow"],
                peak_flow=item["peak_flow"],
                time_to_peak_seconds=item["time_to_peak_seconds"],
                flow_15s=item["flow_15s"],
                flow_30s=item["flow_30s"],
                flow_60s=item["flow_60s"],
                flow_120s=item["flow_120s"],
                pct_2_minutes=item["pct_2_minutes"],
                milk_yield_2_minutes=item["milk_yield_2_minutes"],
                flow_rate_at_removal=item["flow_rate_at_removal"],
                duration_seconds=item["duration_seconds"],
                start_seconds=item["start_seconds"],
                identified_at_milking=item["identified_at_milking"],
                final_detaching=item["final_detaching"],
                extra_attachments=item["extra_attachments"],
            )
        )

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
# Idle time between attachments: count gaps longer than 5 minutes, ignore > 1 hour.
ATTACHMENT_IDLE_MIN_SECONDS = 5 * 60
ATTACHMENT_IDLE_MAX_SECONDS = 60 * 60


def sum_attachment_idle_gaps(
    abs_starts: list[int],
    *,
    min_seconds: int = ATTACHMENT_IDLE_MIN_SECONDS,
    max_seconds: int = ATTACHMENT_IDLE_MAX_SECONDS,
) -> tuple[int, int]:
    """Sum idle gaps between consecutive attachments (first → last).

    Returns ``(total_idle_seconds, gap_count)``. Only gaps strictly longer than
    ``min_seconds`` and at most ``max_seconds`` are included (longer gaps are
    treated as data errors / session breaks).
    """
    if len(abs_starts) < 2:
        return 0, 0
    ordered = sorted(int(s) for s in abs_starts)
    total = 0
    count = 0
    for earlier, later in zip(ordered, ordered[1:]):
        gap = later - earlier
        if min_seconds < gap <= max_seconds:
            total += gap
            count += 1
    return total, count


def attachment_idle_from_clock_starts(
    start_seconds_list: list[int | None],
    *,
    min_seconds: int = ATTACHMENT_IDLE_MIN_SECONDS,
    max_seconds: int = ATTACHMENT_IDLE_MAX_SECONDS,
) -> tuple[int, int]:
    """Idle gaps from milking clock times (handles midnight wrap within a shift)."""
    starts = [int(s) for s in start_seconds_list if s is not None]
    if len(starts) < 2:
        return 0, 0
    origin = shift_timeline_origin(starts)
    if origin is None:
        return 0, 0
    abs_starts = [to_absolute_start(s, origin) for s in starts]
    return sum_attachment_idle_gaps(
        abs_starts, min_seconds=min_seconds, max_seconds=max_seconds
    )


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
    times = [abs_starts[i] for i in order]
    order_pens = [pens[i] for i in order]
    corrected = list(pens)
    changes = 0

    # Sliding window over sorted starts — O(n) after the sort.
    left = 0
    right = 0
    counts: Counter[int] = Counter()
    known_in_window = 0

    for rank, i in enumerate(order):
        t = times[rank]
        while right < n and times[right] <= t + window_seconds:
            p = order_pens[right]
            if p is not None:
                counts[p] += 1
                known_in_window += 1
            right += 1
        while left < n and times[left] < t - window_seconds:
            p = order_pens[left]
            if p is not None:
                counts[p] -= 1
                if counts[p] <= 0:
                    del counts[p]
                known_in_window -= 1
            left += 1

        recorded = pens[i]
        if recorded is None or known_in_window < min_neighbours:
            continue
        majority, count = counts.most_common(1)[0]
        if majority != recorded and (count / known_in_window) >= min_share:
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
