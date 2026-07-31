"""Parse Buitelaar self-billing invoice / payment advice PDFs.

Columns used: Tag Number, Wgt, Amount.
Wgt is stored as cold_weight_kg (liveweight for calves) and Amount as amount_gbp.
Sale date is the Collection Date from the invoice header.
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
    normalize_etag,
)

_COLLECTION_DATE_RE = re.compile(
    r"Collection\s*Date\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
# Tag … Grade Wgt DLWG Amount  e.g. "O+ 56 0.53 £365.00"
# Allow mojibake / replacement currency glyphs before the amount.
_LINE_TAIL_RE = re.compile(
    r"(?P<weight>\d{1,3}(?:\.\d{1,2})?)\s+"
    r"(?P<dlwg>\d+(?:\.\d+)?)\s+"
    r"[^\d]*?(?P<amount>[\d,]+\.\d{2})\s*$",
)
_SKIP_LINE_MARKERS = (
    "no of calves",
    "net total",
    "gross total",
    "total deductions",
    "amount payable",
    "tag number",
    "registered office",
)


def looks_like_buitelaar_pdf(text: str) -> bool:
    """True for Buitelaar self-billing calf payment advices."""
    low = text.lower()
    if "buitelaar" not in low:
        return False
    if "self billing" in low or "payment advice" in low:
        return True
    return "tag number" in low and "wgt" in low and "dlwg" in low


def _farm_from_buitelaar(
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


def _collection_date(text: str) -> dt.date | None:
    match = _COLLECTION_DATE_RE.search(text)
    if match:
        return _parse_short_date(match.group(1))
    return None


def _sale_line(
    etag: str,
    weight: float,
    amount: float,
    collection_date: dt.date | None,
) -> dict[str, Any]:
    return {
        "etag": etag,
        "cold_weight_kg": round(weight, 2),
        "amount_gbp": round(amount, 2),
        "reject_kg": None,
        "kill_date": collection_date,
        "is_rejected": False,
    }


def _parse_text_lines(
    text: str,
    *,
    collection_date: dt.date | None,
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
        tail = _LINE_TAIL_RE.search(after_tag)
        if not tail:
            continue
        weight = _to_float(tail.group("weight"))
        amount = _to_float(tail.group("amount"))
        if weight is None or amount is None:
            warnings.append(f"Skipped incomplete Buitelaar row for {etag}")
            continue
        if not is_acceptable_sale_line(weight, None, amount):
            warnings.append(f"Skipped implausible Buitelaar row for {etag}")
            continue
        seen.add(etag)
        lines.append(_sale_line(etag, weight, amount, collection_date))
    return lines


def parse_buitelaar_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    fallback_sale_date: dt.date | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    """Parse a Buitelaar self-billing invoice PDF.

    Returns ``{farm, sale_date, lines, warnings}`` with the same line shape as
    Eurofarm / Pathway parses so cattle sales matching stays unchanged.
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

    if not looks_like_buitelaar_pdf(text):
        warnings.append("PDF does not look like a Buitelaar remittance")

    sale_date = _collection_date(text) or fallback_sale_date
    farm = _farm_from_buitelaar(
        mailbox_farm=mailbox_farm, source_file=source_file, text=text
    )
    lines = _parse_text_lines(text, collection_date=sale_date, warnings=warnings)

    by_etag: dict[str, dict[str, Any]] = {}
    for line in lines:
        by_etag[line["etag"]] = line
    lines = list(by_etag.values())

    if not lines:
        warnings.append("No sale lines extracted from Buitelaar PDF")
    if sale_date is None:
        warnings.append("Could not parse collection date from Buitelaar PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
    }
