"""Parse monthly milk buyer payment statement PDFs (Freshways / Dairy Partners).

Each buyer emails a PDF statement per farm per month. Field layouts differ:
* Freshways (GAD): litres, quality averages (fat/protein/bacto/SCC/FPD), price
  derived from Amount Payable / Total Litres.
* Dairy Partners (CM): litres, quality averages (fat/protein/bacto/SCC/thermo),
  headline milk price includes haulage which we subtract.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

SUPPLIER_FRESHWAYS = "freshways"
SUPPLIER_DAIRY_PARTNERS = "dairy_partners"

_FRESHWAYS_MARKERS = ("freshways.co.uk", "nijjar")
_DAIRY_PARTNERS_MARKERS = ("dairy partners", "dairypartners.co.uk")

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.replace(",", "").strip())
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "").strip())
    except ValueError:
        return None


def _parse_short_date(value: str) -> dt.date | None:
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _detect_supplier(text: str) -> str | None:
    low = text.lower()
    if any(marker in low for marker in _FRESHWAYS_MARKERS):
        return SUPPLIER_FRESHWAYS
    if any(marker in low for marker in _DAIRY_PARTNERS_MARKERS):
        return SUPPLIER_DAIRY_PARTNERS
    return None


def _extract_text(content: bytes) -> str:
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
    return "\n".join(lines)


def _parse_freshways(text: str) -> dict[str, Any]:
    warnings: list[str] = []
    fields: dict[str, Any] = {
        "farm": "GAD",
        "supplier": SUPPLIER_FRESHWAYS,
        "thermoduric": None,
        "haulage_ppl": None,
    }

    period = re.search(
        r"Milk\s+Statement\s+from\s*(\d{2}/\d{2}/\d{2,4})\s+To\s+(\d{2}/\d{2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    if period:
        start = _parse_short_date(period.group(1))
        if start:
            fields["statement_month"] = _month_start(start)
    if not fields.get("statement_month"):
        warnings.append("Could not determine statement month from Freshways PDF")

    litres = None
    if match := re.search(r"Base\s+Price\s*:\s*([\d,]+)\s*Ltrs?", text, re.IGNORECASE):
        litres = _to_int(match.group(1))
    if litres is None and (match := re.search(r"Total\s+Litres\s+([\d,]+)", text, re.IGNORECASE)):
        litres = _to_int(match.group(1))
    fields["litres_sold"] = litres
    if litres is None:
        warnings.append("Could not parse litres sold from Freshways PDF")

    amount = None
    if match := re.search(
        r"Amount\s+Payable\s+[^\d]*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE
    ):
        amount = _to_float(match.group(1))
    if amount is not None and litres:
        fields["milk_price_ppl"] = round((amount / litres) * 100, 3)
    elif match := re.search(r"\b(3[01]\.\d{3})\b", text):
        # Fallback: headline pence-per-litre in the payment block.
        fields["milk_price_ppl"] = float(match.group(1))
    else:
        fields["milk_price_ppl"] = None
        warnings.append("Could not derive milk price from Freshways PDF")

    # pdfplumber order: Average fat protein bacto scc fpd
    if match := re.search(
        r"Average\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s+(\d+)\s+(\d+)\s+(\d+)",
        text,
        re.IGNORECASE,
    ):
        fields["butterfat_pct"] = float(match.group(1))
        fields["protein_pct"] = float(match.group(2))
        fields["bactoscan"] = int(match.group(3))
        fields["scc"] = int(match.group(4))
        fields["fpd"] = int(match.group(5))
    else:
        warnings.append("Could not parse quality averages from Freshways PDF")

    return {"fields": fields, "warnings": warnings}


def _parse_dairy_partners(text: str, *, default_haulage: float) -> dict[str, Any]:
    warnings: list[str] = []
    fields: dict[str, Any] = {
        "farm": "CM",
        "supplier": SUPPLIER_DAIRY_PARTNERS,
        "fpd": None,
    }

    month_match = re.search(
        r"Payment\s+Month\s*:\s*"
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if month_match:
        try:
            parsed = dt.datetime.strptime(
                f"{month_match.group(1)} {month_match.group(2)}", "%B %Y"
            )
            fields["statement_month"] = parsed.date().replace(day=1)
        except ValueError:
            pass
    if not fields.get("statement_month"):
        warnings.append("Could not determine statement month from Dairy Partners PDF")

    month_name = None
    if month_match:
        month_name = month_match.group(1)[:3]

    litres = None
    quality = None
    if month_name:
        row = re.search(
            rf"^{month_name}\s+"
            r"(\d+\.\d{2})\s+(\d+\.\d{2})\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d,]+)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if row:
            fields["butterfat_pct"] = float(row.group(1))
            fields["protein_pct"] = float(row.group(2))
            fields["bactoscan"] = int(row.group(3))
            fields["scc"] = int(row.group(4))
            fields["thermoduric"] = int(row.group(5))
            litres = _to_int(row.group(6))
            quality = True

    if quality is None and (row := re.search(
        r"Current\s+Year\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s+(\d+)\s+(\d+)\s+(\d+)",
        text,
        re.IGNORECASE,
    )):
        fields["butterfat_pct"] = float(row.group(1))
        fields["protein_pct"] = float(row.group(2))
        fields["bactoscan"] = int(row.group(3))
        fields["scc"] = int(row.group(4))
        fields["thermoduric"] = int(row.group(5))

    if litres is None and month_name:
        warnings.append("Could not parse litres sold from Dairy Partners PDF")
    fields["litres_sold"] = litres

    haulage = default_haulage
    if match := re.search(r"Haulage\s+([\d.]+)", text, re.IGNORECASE):
        parsed = _to_float(match.group(1))
        if parsed is not None:
            haulage = parsed
    fields["haulage_ppl"] = haulage

    headline_price = None
    if match := re.search(
        r"Net\s+Payment\s+to\s+Bank\s*:\s*([\d.]+)", text, re.IGNORECASE
    ):
        headline_price = float(match.group(1))
    if headline_price is not None:
        fields["milk_price_ppl"] = round(headline_price - haulage, 3)
    else:
        fields["milk_price_ppl"] = None
        warnings.append("Could not parse milk price from Dairy Partners PDF")

    if quality is None:
        warnings.append("Could not parse quality averages from Dairy Partners PDF")

    return {"fields": fields, "warnings": warnings}


_REQUIRED_FIELDS = ("farm", "statement_month", "litres_sold", "milk_price_ppl")


def parse_milk_statement_pdf(
    content: bytes, *, default_haulage: float = 1.0
) -> dict[str, Any]:
    """Parse a milk buyer statement PDF.

    Returns {"fields": {...}, "warnings": [...], "supplier": str|None}.
    If required fields are missing, warnings are populated for the importer to skip.
    """
    text = _extract_text(content)
    supplier = _detect_supplier(text)
    if supplier == SUPPLIER_FRESHWAYS:
        result = _parse_freshways(text)
    elif supplier == SUPPLIER_DAIRY_PARTNERS:
        result = _parse_dairy_partners(text, default_haulage=default_haulage)
    else:
        return {
            "fields": {},
            "warnings": ["Unrecognised milk statement PDF (not Freshways or Dairy Partners)"],
            "supplier": None,
        }

    fields = result["fields"]
    warnings = list(result["warnings"])
    for key in _REQUIRED_FIELDS:
        if not fields.get(key) and fields.get(key) != 0:
            if not any(key in w for w in warnings):
                warnings.append(f"Missing required field: {key}")

    return {"fields": fields, "warnings": warnings, "supplier": supplier}
