"""Parse Pickstock Telford cull-cow remittance (kill sheet) PDFs.

Columns used: Eartag, Cold wt, Weight, Value.
Cold wt is stored as cold_weight_kg and Value as amount_gbp.
Weight is the payable kg (usually equal to cold wt); cond wt maps to reject_kg.
Sale date is the Kill Date from the remittance header.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.services.cattle_sale_pdf import (
    _TAG_RE,
    _extract_text,
    _parse_short_date,
    _to_float,
    is_acceptable_sale_line,
    is_rejected_sale,
    normalize_etag,
)

_KILL_DATE_RE = re.compile(
    r"Kill\s*Date\s*:\s*([^\n]+)",
    re.IGNORECASE,
)
_HOT_WT_RE = re.compile(r"^\d{2,3}\.\d{2}$")
_SKIP_LINE_MARKERS = (
    "kill no",
    "vendor vat",
    "vendor code",
    "no. in batch",
    "amount payable",
    "total charges",
    "it is your responsibility",
    "www.pickstock",
)
_DATE_FORMATS = (
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d/%m/%Y",
    "%d/%m/%y",
)


def looks_like_pickstock_pdf(text: str) -> bool:
    """True for Pickstock Telford cull-cow remittances / kill sheets."""
    low = text.lower()
    compact = re.sub(r"\s+", "", low)
    if "pickstock" in compact:
        return True
    return (
        "kill date" in low
        and "eartag" in compact
        and "cold" in low
        and "p/kg" in compact
        and "vendor code" in low
    )


def _farm_from_pickstock(
    *,
    mailbox_farm: str | None,
    source_file: str | None,
    text: str,
) -> str | None:
    if mailbox_farm:
        return mailbox_farm
    name_low = (source_file or "").lower()
    if "gad" in name_low or "green acre" in name_low:
        return "GAD"
    if "cm" in name_low or "cwrt" in name_low or "malle" in name_low:
        return "CM"
    farm_text = text.lower()
    if "green acre" in farm_text:
        return "GAD"
    if "cwrt malle" in farm_text or "cwrtmalle" in farm_text:
        return "CM"
    return None


def _parse_named_date(value: str) -> dt.date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = _parse_short_date(raw)
    if parsed:
        return parsed
    tokens = raw.replace(",", " ").split()
    if len(tokens) >= 3:
        chunk = " ".join(tokens[:3])
        if tokens[0].lower().startswith("sept") and tokens[0].lower() != "september":
            chunk = "Sep " + " ".join(tokens[1:3])
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(chunk, fmt).date()
            except ValueError:
                continue
    return None


def _kill_date(text: str) -> dt.date | None:
    match = _KILL_DATE_RE.search(text)
    if match:
        return _parse_named_date(match.group(1))
    return None


def _sale_line(
    etag: str,
    weight: float,
    amount: float,
    reject_kg: float | None,
    kill_date: dt.date | None,
) -> dict[str, Any]:
    return {
        "etag": etag,
        "cold_weight_kg": round(weight, 2),
        "amount_gbp": round(amount, 2),
        "reject_kg": round(reject_kg, 2) if reject_kg is not None else None,
        "kill_date": kill_date,
        "is_rejected": is_rejected_sale(weight, reject_kg, amount),
    }


def _hot_weight_index(tokens: list[str]) -> int | None:
    for idx, token in enumerate(tokens):
        cleaned = token.replace(",", "")
        if not _HOT_WT_RE.match(cleaned):
            continue
        value = _to_float(token)
        if value is not None and 50.0 <= value <= 900.0:
            return idx
    return None


def _parse_row_numbers(
    after_tag: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (cold_wt, payable_weight, reject_kg, amount) after the eartag.

    Typical layout after grade: Hot wt, Rebate, Cold wt, [Cond wt], Weight, p/Kg, Value.
    Cond wt is omitted when zero, so there are either 6 or 7 numeric fields.
    """
    tokens = after_tag.split()
    start = _hot_weight_index(tokens)
    if start is None:
        return None, None, None, None
    nums = [_to_float(token) for token in tokens[start:]]
    nums = [n for n in nums if n is not None]
    if len(nums) < 4:
        return None, None, None, None
    amount = nums[-1]
    if len(nums) >= 5 and 1.0 <= abs(nums[-2]) <= 15.0:
        body = nums[:-2]
    else:
        body = nums[:-1]
    if len(body) < 3:
        return None, None, None, None
    cold = body[2]
    payable = cold
    reject_kg = None
    if len(body) == 4:
        payable = body[3]
    elif len(body) >= 5:
        cond = body[3]
        payable = body[4]
        if cond is not None and abs(cond) > 0.005:
            reject_kg = abs(cond)
    return cold, payable, reject_kg, amount


def _parse_text_lines(
    text: str,
    *,
    kill_date: dt.date | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        low = raw_line.lower()
        if any(marker in low for marker in _SKIP_LINE_MARKERS):
            continue
        tag_match = _TAG_RE.search(raw_line)
        if not tag_match:
            continue
        etag = normalize_etag(tag_match.group(0))
        if etag in seen:
            continue
        after_tag = raw_line[tag_match.end() :]
        cold, _payable, reject_kg, amount = _parse_row_numbers(after_tag)
        if cold is None or amount is None:
            continue
        stored_weight = cold
        if not is_acceptable_sale_line(stored_weight, reject_kg, amount):
            warnings.append(f"Skipped implausible Pickstock row for {etag}")
            continue
        seen.add(etag)
        lines.append(_sale_line(etag, stored_weight, amount, reject_kg, kill_date))
    return lines


def parse_pickstock_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    fallback_sale_date: dt.date | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    """Parse a Pickstock Telford remittance PDF.

    Returns ``{farm, sale_date, lines, warnings}`` with the same line shape as
    Eurofarm / Pathway / Buitelaar / Game Changer parses.
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

    if not looks_like_pickstock_pdf(text):
        warnings.append("PDF does not look like a Pickstock remittance")

    sale_date = _kill_date(text) or fallback_sale_date
    farm = _farm_from_pickstock(
        mailbox_farm=mailbox_farm, source_file=source_file, text=text
    )
    lines = _parse_text_lines(text, kill_date=sale_date, warnings=warnings)

    by_etag: dict[str, dict[str, Any]] = {}
    for line in lines:
        by_etag[line["etag"]] = line
    lines = list(by_etag.values())

    if not lines:
        warnings.append("No sale lines extracted from Pickstock PDF")
    if sale_date is None:
        warnings.append("Could not parse kill date from Pickstock PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
    }
