"""Parse Eurofarm Wales cheque payment report PDFs.

Each report lists sold animals with ear tag, cold weight (kg), kill date, and amount (£).
Farm (CM / GAD) is inferred from the importing mailbox, not the PDF body.
Sale date on each line is the per-animal kill date from the PDF table.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from typing import Any

import pdfplumber

for _noisy in ("pdfminer", "pdfminer.pdffont", "pdfminer.pdfinterp"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_EUROFARM_MARKERS = (
    "eurofarm",
    "euro farm",
    "cheque payment",
    "payment report",
    "livestock purchase remittance",
)
# UK tags are usually contiguous. Foreign tags (BE/DE/FR/IE/…) are often printed
# with a space mid-number in Eurofarm PDFs, e.g. "BE21428 3270".
_TAG_RE = re.compile(
    r"(?:"
    r"UK\s*\d{10,15}"
    r"|"
    r"[A-Z]{2}\s*\d{2,}(?:\s+\d{2,})+"
    r"|"
    r"[A-Z]{2}\s*\d{6,18}"
    r")",
    re.IGNORECASE,
)
_DATE_PATTERNS = (
    re.compile(
        r"(?:cheque|payment|sale|kill|slaughter)\s*date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        re.IGNORECASE,
    ),
    re.compile(r"date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE),
)


def normalize_etag(value: str | None) -> str:
    """Normalize ear tags for matching herd events and Eurofarm remittances.

    DairyComp often zero-pads after the country letters (e.g. BE000214283270)
    while Eurofarm drops those zeros and may insert spaces (BE21428 3270).
    Strip spaces and leading zeros in the numeric section so both forms match.
    Bare numeric UK tags get a UK prefix.
    """
    raw = re.sub(r"\s+", "", (value or "").strip()).upper()
    if not raw:
        return ""
    if not re.match(r"^[A-Z]{2}", raw) and raw.isdigit() and len(raw) >= 10:
        raw = f"UK{raw}"
    match = re.match(r"^([A-Z]{2})0*(\d+)$", raw)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return raw


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    # Strip currency symbols / letters so "£ 460.00" and mojibake pounds parse.
    cleaned = re.sub(r"[^\d.\-]+", "", str(value).replace(",", ""))
    if not cleaned or cleaned in {".", "-", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_short_date(value: str) -> dt.date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_text(content: bytes) -> str:
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
    return "\n".join(lines)


def _extract_tables(content: bytes) -> list[list[list[str | None]]]:
    tables: list[list[list[str | None]]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if table:
                    tables.append(table)
    return tables


def _cell_text(value: str | None) -> str:
    return (value or "").strip()


def _is_qas_cell(value: str | None) -> bool:
    """Eurofarm QAS column is YES or NO (both rows still have weight/amount)."""
    return _cell_text(value).upper() in {"YES", "NO"}


_MIN_CARCASS_KG = 50.0
_MAX_CARCASS_KG = 900.0
_MIN_PRICE_PER_KG = 1.0
_MAX_PRICE_PER_KG = 15.0
# Pathway (and similar) calf liveweights are lighter and £/kg is higher.
_MIN_CALF_KG = 20.0
_MAX_CALF_KG = 200.0
_MIN_CALF_PRICE_PER_KG = 1.0
_MAX_CALF_PRICE_PER_KG = 30.0


def is_plausible_carcass_row(weight: float | None, amount: float | None) -> bool:
    """Reject rows where cold weight was confused with price/kg (typically 2–8)."""
    if weight is None or amount is None:
        return False
    if weight < _MIN_CARCASS_KG or weight > _MAX_CARCASS_KG:
        return False
    price_per_kg = amount / weight
    return _MIN_PRICE_PER_KG <= price_per_kg <= _MAX_PRICE_PER_KG


def is_plausible_calf_row(weight: float | None, amount: float | None) -> bool:
    """Liveweight calf sales (e.g. Pathway Farming remittances)."""
    if weight is None or amount is None:
        return False
    if weight < _MIN_CALF_KG or weight > _MAX_CALF_KG:
        return False
    if amount < 0:
        return False
    if abs(amount) <= 0.005:
        return True
    price_per_kg = amount / weight
    return _MIN_CALF_PRICE_PER_KG <= price_per_kg <= _MAX_CALF_PRICE_PER_KG


def _weights_match(a: float, b: float, tolerance: float = 0.05) -> bool:
    return abs(a - b) <= tolerance


def is_rejected_sale(
    cold_weight_kg: float | None,
    reject_kg: float | None,
    amount_gbp: float | None,
) -> bool:
    """Animal rejected at abattoir: zero payment and cold weight equals reject kgs."""
    if cold_weight_kg is None or reject_kg is None or amount_gbp is None:
        return False
    if abs(amount_gbp) > 0.005:
        return False
    if cold_weight_kg < _MIN_CARCASS_KG or cold_weight_kg > _MAX_CARCASS_KG:
        return False
    return _weights_match(cold_weight_kg, reject_kg)


def is_acceptable_sale_line(
    cold_weight_kg: float | None,
    reject_kg: float | None,
    amount_gbp: float | None,
) -> bool:
    return (
        is_plausible_carcass_row(cold_weight_kg, amount_gbp)
        or is_plausible_calf_row(cold_weight_kg, amount_gbp)
        or is_rejected_sale(cold_weight_kg, reject_kg, amount_gbp)
    )


def _header_indices(header_row: list[str | None]) -> dict[str, int] | None:
    labels = [_cell_text(c).lower() for c in header_row]
    tag_idx = None
    weight_idx = None
    reject_idx = None
    kill_idx = None
    amount_idx = None
    for idx, label in enumerate(labels):
        if not label:
            continue
        if tag_idx is None and (
            "ear tag" in label
            or "tag number" in label
            or label in {"tag", "eartag", "ear tag no", "tag no"}
        ):
            tag_idx = idx
        if weight_idx is None and ("cold" in label and "weight" in label):
            weight_idx = idx
        elif weight_idx is None and label in {"cold wt", "cold weight", "weight kg", "weight (kg)", "kg"}:
            weight_idx = idx
        if reject_idx is None and "reject" in label:
            reject_idx = idx
        if kill_idx is None and "kill" in label and "date" in label:
            kill_idx = idx
        if amount_idx is None and label in {"amount", "value", "total", "payment", "£"}:
            amount_idx = idx
        elif amount_idx is None and "amount" in label:
            amount_idx = idx
    if tag_idx is None:
        return None
    return {
        "tag": tag_idx,
        "weight": weight_idx,
        "reject": reject_idx,
        "kill": kill_idx,
        "amount": amount_idx,
    }


def _find_tag_in_row(row: list[str | None]) -> tuple[int, str] | None:
    for idx, cell in enumerate(row):
        tag_match = _TAG_RE.search(_cell_text(cell))
        if tag_match:
            return idx, normalize_etag(tag_match.group(0))
    return None


def _find_table_header(table: list[list[str | None]]) -> tuple[dict[str, int], int] | None:
    for idx, row in enumerate(table):
        joined = " ".join(_cell_text(c).lower() for c in row)
        if "tag" in joined and ("weight" in joined or "cold" in joined or "amount" in joined):
            header = _header_indices(row)
            if header is not None:
                return header, idx
    return None


def _parse_row_numbers_from_cells(
    row: list[str | None],
    header: dict[str, int],
    etag: str,
    warnings: list[str],
) -> tuple[float | None, float | None, float | None]:
    """Recover weight, amount, and reject kgs when header columns are missing or wrong."""
    for idx, cell in enumerate(row):
        if _is_qas_cell(cell):
            nums = [
                v
                for c in row[idx + 1 :]
                if (v := _to_float(_cell_text(c))) is not None
            ]
            if len(nums) >= 4:
                weight, reject_kg, _price_kg, amount = nums[0], nums[1], nums[2], nums[3]
                if is_acceptable_sale_line(weight, reject_kg, amount):
                    return weight, amount, reject_kg
            if len(nums) >= 3:
                weight, amount = nums[0], nums[-1]
                reject_kg = nums[1] if len(nums) == 4 else None
                if is_acceptable_sale_line(weight, reject_kg, amount):
                    return weight, amount, reject_kg

    floats: list[tuple[int, float]] = []
    for idx, cell in enumerate(row):
        if idx == header.get("tag"):
            continue
        value = _to_float(_cell_text(cell))
        if value is not None:
            floats.append((idx, value))
    if not floats:
        return None, None, None

    amount = None
    if header.get("amount") is not None and header["amount"] < len(row):
        amount = _to_float(_cell_text(row[header["amount"]]))
    if amount is None:
        amount = floats[-1][1]

    reject_kg = None
    if header.get("reject") is not None and header["reject"] < len(row):
        reject_kg = _to_float(_cell_text(row[header["reject"]]))

    weight = None
    if header.get("weight") is not None and header["weight"] < len(row):
        weight = _to_float(_cell_text(row[header["weight"]]))
    if weight is None or not is_acceptable_sale_line(weight, reject_kg, amount):
        for _idx, value in reversed(floats):
            if value == amount:
                continue
            if is_acceptable_sale_line(value, reject_kg, amount):
                weight = value
                break
    if weight is None:
        warnings.append(f"Could not find cold weight for {etag}")
    return weight, amount, reject_kg


def _parse_kill_date_from_row(
    row: list[str | None],
    header: dict[str, int],
) -> dt.date | None:
    kill_idx = header.get("kill")
    if kill_idx is not None and kill_idx < len(row):
        parsed = _parse_short_date(_cell_text(row[kill_idx]))
        if parsed:
            return parsed
    # Eurofarm continuation rows often shift columns; kill date sits before QAS.
    qas_idx = next(
        (idx for idx, cell in enumerate(row) if _is_qas_cell(cell)),
        None,
    )
    scan = row[:qas_idx] if qas_idx is not None else row
    for cell in reversed(scan):
        parsed = _parse_short_date(_cell_text(cell))
        if parsed:
            return parsed
    return None


def _sale_line_dict(
    etag: str,
    weight: float,
    amount: float,
    reject_kg: float | None,
    kill_date: dt.date | None = None,
) -> dict[str, Any]:
    rejected = is_rejected_sale(weight, reject_kg, amount)
    return {
        "etag": etag,
        "cold_weight_kg": round(weight, 2),
        "amount_gbp": round(amount, 2),
        "reject_kg": round(reject_kg, 2) if reject_kg is not None else None,
        "kill_date": kill_date,
        "is_rejected": rejected,
    }


def _parse_data_row(
    row: list[str | None],
    header: dict[str, int],
    warnings: list[str],
) -> dict[str, Any] | None:
    tag_hit = _find_tag_in_row(row)
    if tag_hit is None:
        return None
    tag_idx, etag = tag_hit
    row_header = {**header, "tag": tag_idx}

    # Prefer QAS-anchored numbers: continuation tables often shift columns so
    # Age/QAS land under Cold Weight / Amount and look "plausible" (e.g. 79 kg).
    weight, amount, reject_kg = _parse_row_numbers_from_cells(
        row, row_header, etag, warnings
    )
    if not is_acceptable_sale_line(weight, reject_kg, amount):
        reject_kg = None
        if header.get("reject") is not None and header["reject"] < len(row):
            reject_kg = _to_float(_cell_text(row[header["reject"]]))
        weight = None
        amount = None
        if header.get("weight") is not None and header["weight"] < len(row):
            weight = _to_float(_cell_text(row[header["weight"]]))
        if header.get("amount") is not None and header["amount"] < len(row):
            amount = _to_float(_cell_text(row[header["amount"]]))
    if weight is None or amount is None or not is_acceptable_sale_line(weight, reject_kg, amount):
        warnings.append(f"Skipped implausible row for {etag}")
        return None
    kill_date = _parse_kill_date_from_row(row, header)
    return _sale_line_dict(etag, weight, amount, reject_kg, kill_date)


def _parse_table_rows(
    table: list[list[str | None]],
    warnings: list[str],
    *,
    shared_header: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    if not table:
        return [], shared_header

    header = shared_header
    header_row_idx = -1
    if header is None:
        found = _find_table_header(table)
        if found is None:
            return [], None
        header, header_row_idx = found

    start = header_row_idx + 1 if header_row_idx >= 0 else 0
    lines: list[dict[str, Any]] = []
    for row in table[start:]:
        if not row:
            continue
        parsed = _parse_data_row(row, header, warnings)
        if parsed is not None:
            lines.append(parsed)
    return lines, header


def _parse_text_lines(text: str, warnings: list[str]) -> list[dict[str, Any]]:
    """Fallback: scan text lines for tag, cold weight, and amount.

    Eurofarm remittance lines end with: YES|NO <weight> <reject> <price/kg> <amount>.
    """
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    row_re = re.compile(
        r"(?:UK\s*\d{10,15}|[A-Z]{2}\s*\d{2,}(?:\s+\d{2,})+|[A-Z]{2}\s*\d{6,18})"
        r".*?(?:YES|NO)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+[\d,]+\.?\d*\s+([\d,]+\.?\d*)",
        re.IGNORECASE,
    )
    for raw_line in text.splitlines():
        tag_match = _TAG_RE.search(raw_line)
        if not tag_match:
            continue
        etag = normalize_etag(tag_match.group(0))
        if etag in seen:
            continue
        weight = None
        reject_kg = None
        amount = None
        structured = row_re.search(raw_line)
        if structured:
            weight = _to_float(structured.group(1))
            reject_kg = _to_float(structured.group(2))
            amount = _to_float(structured.group(3))
        if weight is None or amount is None:
            after_tag = raw_line[tag_match.end() :]
            floats = [f for n in re.findall(r"[\d,]+\.?\d*", after_tag) if (f := _to_float(n)) is not None]
            if len(floats) < 2:
                continue
            amount = floats[-1]
            if len(floats) >= 4:
                weight = floats[0]
                reject_kg = floats[1]
            else:
                candidates = [f for f in floats[:-1] if 50 <= f <= 1500]
                weight = max(candidates) if candidates else floats[-2]
        if weight is None or amount is None:
            continue
        if not is_acceptable_sale_line(weight, reject_kg, amount):
            continue
        kill_date = None
        kill_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{2,4})\s+\d+\s+(?:YES|NO)\b",
            raw_line,
            re.IGNORECASE,
        )
        if kill_match:
            kill_date = _parse_short_date(kill_match.group(1))
        seen.add(etag)
        lines.append(_sale_line_dict(etag, weight, amount, reject_kg, kill_date))
    if not lines:
        warnings.append("No animal rows found in PDF text fallback")
    return lines


def _parse_sale_date(text: str, fallback: dt.date | None) -> dt.date | None:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            parsed = _parse_short_date(match.group(1))
            if parsed:
                return parsed
    return fallback


def _is_eurofarm_report(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _EUROFARM_MARKERS)


def _farm_from_source(*, source_file: str | None, text: str, mailbox_farm: str | None) -> str | None:
    if mailbox_farm:
        return mailbox_farm
    name_low = (source_file or "").lower()
    if "gad" in name_low or "green acre" in name_low:
        return "GAD"
    if "cm" in name_low or "cwrt" in name_low or "malle" in name_low:
        return "CM"
    text_low = text.lower()
    if "green acre" in text_low:
        return "GAD"
    if "cwrt malle" in text_low or "cwrtmalle" in text_low:
        return "CM"
    return None


def parse_cattle_sale_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    fallback_sale_date: dt.date | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    """Parse a Eurofarm cheque payment PDF.

    Returns ``{farm, sale_date, lines, warnings}``. ``farm`` comes from
    ``mailbox_farm`` when supplied (CM / GAD).
    """
    warnings: list[str] = []
    text = _extract_text(content)
    if not text.strip():
        return {
            "farm": mailbox_farm,
            "sale_date": fallback_sale_date,
            "lines": [],
            "warnings": ["PDF contained no extractable text"],
        }

    if not _is_eurofarm_report(text):
        warnings.append("PDF does not look like a Eurofarm payment report")

    sale_date = _parse_sale_date(text, fallback_sale_date)
    farm = _farm_from_source(source_file=source_file, text=text, mailbox_farm=mailbox_farm)

    lines: list[dict[str, Any]] = []
    shared_header: dict[str, int] | None = None
    for table in _extract_tables(content):
        table_lines, shared_header = _parse_table_rows(
            table, warnings, shared_header=shared_header
        )
        lines.extend(table_lines)

    if not lines:
        lines = _parse_text_lines(text, warnings)

    # Deduplicate by etag within one PDF (last row wins).
    by_etag: dict[str, dict[str, Any]] = {}
    for line in lines:
        by_etag[line["etag"]] = line
    lines = list(by_etag.values())

    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None and not any(line.get("kill_date") for line in lines):
        warnings.append("Could not parse sale/kill date from PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
    }
