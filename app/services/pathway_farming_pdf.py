"""Parse Pathway Farming calf remittance / invoice PDFs.

Columns used: Eartag, CollectionDate, Weight, Calf Cost.
Weight is stored as cold_weight_kg (liveweight for calves) and calf cost as amount_gbp.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.services.cattle_sale_pdf import (
    _TAG_RE,
    _cell_text,
    _extract_tables,
    _extract_text,
    _parse_short_date,
    _to_float,
    is_acceptable_sale_line,
    normalize_etag,
)

_PATHWAY_MARKERS = (
    "calf cost",
    "remittance/ invoice",
    "remittance/invoice",
)

_HEADER_DATE_RE = re.compile(
    r"(?:^|\n)\s*Date\s+(\d{1,2}/\d{1,2}/\d{2,4})\b",
    re.IGNORECASE,
)
_COLLECTION_FARM_RE = re.compile(
    r"Collection\s*farm\s+([^\n]+)",
    re.IGNORECASE,
)


def looks_like_pathway_pdf(text: str) -> bool:
    """True for Pathway calf remittances, not grazing self-bills or feed invoices."""
    low = text.lower()
    compact = re.sub(r"\s+", "", low)
    if "calfcost" in compact:
        return True
    if any(marker in low for marker in _PATHWAY_MARKERS):
        return True
    return (
        "collectionfarm" in compact
        and "eartag" in compact
        and "weight" in low
        and "invoice number" in low
    )


def _farm_from_pathway(
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
    farm_match = _COLLECTION_FARM_RE.search(text)
    farm_text = (farm_match.group(1) if farm_match else text).lower()
    if "green acre" in farm_text:
        return "GAD"
    if "cwrt malle" in farm_text or "cwrtmalle" in farm_text:
        return "CM"
    return None


def _invoice_date(text: str) -> dt.date | None:
    match = _HEADER_DATE_RE.search(text)
    if match:
        return _parse_short_date(match.group(1))
    return None


def _header_indices(header_row: list[str | None]) -> dict[str, int] | None:
    labels = [re.sub(r"\s+", "", _cell_text(c).lower()) for c in header_row]
    tag_idx = next(
        (
            i
            for i, label in enumerate(labels)
            if label in {"eartag", "ear tag", "tag", "tagnumber"} or "eartag" in label
        ),
        None,
    )
    date_idx = next(
        (
            i
            for i, label in enumerate(labels)
            if "collectiondate" in label or label in {"date", "collection"}
        ),
        None,
    )
    weight_idx = next(
        (i for i, label in enumerate(labels) if "weight" in label),
        None,
    )
    amount_idx = next(
        (
            i
            for i, label in enumerate(labels)
            if "calfcost" in label or "cost" in label or "amount" in label
        ),
        None,
    )
    if tag_idx is None or weight_idx is None or amount_idx is None:
        return None
    return {
        "tag": tag_idx,
        "date": date_idx if date_idx is not None else -1,
        "weight": weight_idx,
        "amount": amount_idx,
    }


def _find_header(table: list[list[str | None]]) -> tuple[dict[str, int], int] | None:
    for idx, row in enumerate(table):
        joined = " ".join(_cell_text(c).lower() for c in row)
        if "eartag" in joined.replace(" ", "") or (
            "tag" in joined and "weight" in joined and ("cost" in joined or "amount" in joined)
        ):
            header = _header_indices(row)
            if header is not None:
                return header, idx
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


def _parse_table_rows(
    table: list[list[str | None]],
    *,
    header: dict[str, int] | None,
    fallback_date: dt.date | None,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    start = 0
    active_header = header
    if active_header is None:
        found = _find_header(table)
        if found is None:
            return [], None
        active_header, header_row_idx = found
        start = header_row_idx + 1

    lines: list[dict[str, Any]] = []
    for row in table[start:]:
        if not row:
            continue
        tag_cell = (
            _cell_text(row[active_header["tag"]])
            if active_header["tag"] < len(row)
            else ""
        )
        tag_match = _TAG_RE.search(tag_cell) or _TAG_RE.search(
            " ".join(_cell_text(c) for c in row)
        )
        if not tag_match:
            continue
        etag = normalize_etag(tag_match.group(0))
        weight = (
            _to_float(_cell_text(row[active_header["weight"]]))
            if active_header["weight"] < len(row)
            else None
        )
        amount = (
            _to_float(_cell_text(row[active_header["amount"]]))
            if active_header["amount"] < len(row)
            else None
        )
        collection_date = fallback_date
        date_idx = active_header.get("date", -1)
        if date_idx is not None and date_idx >= 0 and date_idx < len(row):
            parsed = _parse_short_date(_cell_text(row[date_idx]))
            if parsed:
                collection_date = parsed
        if weight is None or amount is None:
            warnings.append(f"Skipped incomplete Pathway row for {etag}")
            continue
        if not is_acceptable_sale_line(weight, None, amount):
            warnings.append(f"Skipped implausible Pathway row for {etag}")
            continue
        lines.append(_sale_line(etag, weight, amount, collection_date))
    return lines, active_header


def _parse_text_lines(
    text: str,
    *,
    fallback_date: dt.date | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        if "total" in raw_line.lower() and "av." in raw_line.lower():
            continue
        tag_match = _TAG_RE.search(raw_line)
        if not tag_match:
            continue
        etag = normalize_etag(tag_match.group(0))
        if etag in seen:
            continue
        # Prefer trailing weight + money pair.
        money_matches = list(
            re.finditer(
                r"(?P<weight>\d{1,3}(?:\.\d{1,2})?)\s+(?:£|\u00a3)?\s*(?P<amount>[\d,]+\.\d{2})\s*$",
                raw_line,
            )
        )
        if not money_matches:
            continue
        last = money_matches[-1]
        weight = _to_float(last.group("weight"))
        amount = _to_float(last.group("amount"))
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", raw_line)
        collection_date = (
            _parse_short_date(date_match.group(0)) if date_match else fallback_date
        )
        if weight is None or amount is None:
            continue
        if not is_acceptable_sale_line(weight, None, amount):
            warnings.append(f"Skipped implausible Pathway text row for {etag}")
            continue
        seen.add(etag)
        lines.append(_sale_line(etag, weight, amount, collection_date))
    return lines


def parse_pathway_farming_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    fallback_sale_date: dt.date | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    """Parse a Pathway Farming calf remittance PDF.

    Returns ``{farm, sale_date, lines, warnings}`` with the same line shape as
    Eurofarm parses so cattle sales / sales payments matching stays unchanged.
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

    if not looks_like_pathway_pdf(text):
        warnings.append("PDF does not look like a Pathway Farming remittance")

    sale_date = _invoice_date(text) or fallback_sale_date
    farm = _farm_from_pathway(
        mailbox_farm=mailbox_farm, source_file=source_file, text=text
    )

    lines: list[dict[str, Any]] = []
    shared_header: dict[str, int] | None = None
    for table in _extract_tables(content):
        table_lines, shared_header = _parse_table_rows(
            table,
            header=shared_header,
            fallback_date=sale_date,
            warnings=warnings,
        )
        lines.extend(table_lines)

    if not lines:
        lines = _parse_text_lines(text, fallback_date=sale_date, warnings=warnings)

    by_etag: dict[str, dict[str, Any]] = {}
    for line in lines:
        by_etag[line["etag"]] = line
    lines = list(by_etag.values())

    if not lines:
        warnings.append("No sale lines extracted from Pathway PDF")
    if sale_date is None and not any(line.get("kill_date") for line in lines):
        warnings.append("Could not parse collection date from Pathway PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
    }
