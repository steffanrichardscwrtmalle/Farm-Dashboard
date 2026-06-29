"""Parse NML (National Milk Laboratories) milk-quality report PDFs.

Each report (one per farm / milk buyer) lists per-collection milk quality
results. The two buyers we receive differ slightly in which trailing columns
are populated:

* Freshways (Green Acre Dairy): the A/B (antibiotic) Pass/Fail column is filled
  and the Urea column is blank.
* Dairy Partners (Cwrt Malle): the Urea column is filled and A/B is blank.

Column order (left to right) is identical for both:
    Sample ID | Sample Date | B/FAT % | Protein % | SCC | BactoScan | FPD |
    A/B (Pass/Fail) | Urea %

"WEIGHTED AVERAGE" daily roll-up rows are ignored.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

_MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()

_MONTH_RE = re.compile(r"^(?:%s)\s+\d{4}$" % "|".join(_MONTHS))
_PRODUCER_RE = re.compile(r"Producer Reference:\s*(\S+)")
_REPORT_DATE_RE = re.compile(r"Report Date:\s*(\d{2}/\d{2}/\d{4})")
_PRODUCTION_UNIT_RE = re.compile(r"Production Unit\s+(.+?)\s*$")
# A data row starts with a sample id (digits, leading zeros kept) then a date.
_DATA_ROW_RE = re.compile(r"^(\d{1,5})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Producer reference -> internal farm code used across the dashboard.
PRODUCER_REF_FARM: dict[str, str] = {
    "9131": "GAD",
    "389000184": "CM",
}


def farm_for_producer_ref(producer_ref: str | None) -> str | None:
    if not producer_ref:
        return None
    return PRODUCER_REF_FARM.get(producer_ref.strip())


def _to_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _clean_number(token: str) -> str | None:
    """Strip accreditation marks etc. and return a numeric string, or None."""
    cleaned = re.sub(r"[^0-9.]", "", token)
    if cleaned and _NUMBER_RE.match(cleaned):
        return cleaned
    return None


def _parse_metadata(lines: list[str], full_text: str) -> dict[str, Any]:
    producer_ref = None
    if match := _PRODUCER_RE.search(full_text):
        producer_ref = match.group(1).strip()

    report_date = None
    if match := _REPORT_DATE_RE.search(full_text):
        report_date = _to_date(match.group(1))

    production_unit = None
    for line in lines:
        if match := _PRODUCTION_UNIT_RE.search(line):
            production_unit = match.group(1).strip()
            break

    notification_type = None
    notif_idx = None
    for idx, line in enumerate(lines):
        if line.strip().endswith("Notification"):
            notification_type = line.strip()
            notif_idx = idx
            break

    report_month = None
    month_idx = None
    for idx, line in enumerate(lines):
        if _MONTH_RE.match(line.strip()):
            report_month = line.strip()
            month_idx = idx
            break

    # The milk buyer is the line between the notification heading and the month.
    milk_buyer = None
    if notif_idx is not None and month_idx is not None and month_idx > notif_idx:
        for line in lines[notif_idx + 1 : month_idx]:
            if line.strip():
                milk_buyer = line.strip()
                break

    return {
        "producer_ref": producer_ref,
        "farm": farm_for_producer_ref(producer_ref),
        "milk_buyer": milk_buyer,
        "notification_type": notification_type,
        "report_month": report_month,
        "report_date": report_date,
        "production_unit": production_unit,
    }


def _parse_data_row(sample_id: str, date_str: str, rest: str) -> dict[str, Any] | None:
    sample_date = _to_date(date_str)
    if sample_date is None:
        return None

    tokens = rest.split()
    numbers: list[str] = []
    antibiotic_pass: bool | None = None
    for token in tokens:
        low = token.lower()
        if low in ("pass", "fail"):
            antibiotic_pass = low == "pass"
            continue
        number = _clean_number(token)
        if number is not None:
            numbers.append(number)

    # Need at least B/FAT, Protein, SCC, BactoScan, FPD.
    if len(numbers) < 5:
        return None

    urea_pct = float(numbers[5]) if len(numbers) >= 6 else None

    return {
        "sample_id": sample_id,
        "sample_date": sample_date,
        "butterfat_pct": float(numbers[0]),
        "protein_pct": float(numbers[1]),
        "scc": int(float(numbers[2])),
        "bactoscan": int(float(numbers[3])),
        "fpd": int(float(numbers[4])),
        "antibiotic_pass": antibiotic_pass,
        "urea_pct": urea_pct,
    }


def parse_nml_pdf(content: bytes) -> dict[str, Any]:
    """Parse a single NML report PDF into metadata + per-sample result rows."""
    all_lines: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.splitlines())

    full_text = "\n".join(all_lines)
    metadata = _parse_metadata(all_lines, full_text)

    samples: list[dict[str, Any]] = []
    for line in all_lines:
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("WEIGHTED AVERAGE"):
            continue
        match = _DATA_ROW_RE.match(stripped)
        if not match:
            continue
        row = _parse_data_row(match.group(1), match.group(2), match.group(3))
        if row is not None:
            samples.append(row)

    return {"metadata": metadata, "samples": samples}
