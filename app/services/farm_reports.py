"""Farm Reports: inventory-based animal lists with widget counts."""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from sqlalchemy import Integer, cast, select
from sqlalchemy.orm import Session

from app.models import HerdInventory
from app.services.farm_schedule import FARM_LABELS, normalize_farm

HEIFERS_TO_SCAN_RC = (3, 4)
HEIFERS_TO_SCAN_MIN_DSLH = 31
WIDGET_HEIFERS_TO_SCAN = "heifers-to-scan"
BLANK_PEN = "__blank__"
NO_MATCH_PEN = "__no_match__"
PDF_CONTENT_TYPE = "application/pdf"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_ROWS_PER_PAGE = 40
TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("remark", "REMARK"),
    ("etag5", "ETAG5"),
    ("dslh", "DSLH"),
    ("tbrd", "TBRD"),
    ("rpro", "RPRO"),
    ("pen", "PEN"),
)


def etag5(etag: str | None) -> str | None:
    """Last five digits of ETAG after trimming leading/trailing spaces."""
    trimmed = (etag or "").strip()
    digits = "".join(ch for ch in trimmed if ch.isdigit())
    return digits[-5:] if digits else None


def _whole_number(value: object) -> int | float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _cell(value: object) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def _pen_sort_key(pen: str) -> tuple[int, int, str]:
    if pen.isdigit():
        return (0, int(pen), "")
    return (1, 0, pen.lower())


def unique_pens(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    pens: list[str] = []
    has_blank = False
    for row in rows:
        pen = (row.get("pen") or "").strip()
        if not pen:
            has_blank = True
            continue
        if pen not in seen:
            seen.add(pen)
            pens.append(pen)
    pens.sort(key=_pen_sort_key)
    items = [{"id": pen, "label": pen} for pen in pens]
    if has_blank:
        items.append({"id": BLANK_PEN, "label": "(blank)"})
    return items


def apply_pen_filter(
    rows: list[dict[str, Any]], pens: list[str] | None
) -> list[dict[str, Any]]:
    if not pens:
        return list(rows)
    if pens == [NO_MATCH_PEN]:
        return []
    selected = {pen for pen in pens if pen and pen != NO_MATCH_PEN}
    include_blank = BLANK_PEN in selected
    selected.discard(BLANK_PEN)
    out: list[dict[str, Any]] = []
    for row in rows:
        pen = (row.get("pen") or "").strip()
        if not pen:
            if include_blank:
                out.append(row)
        elif pen in selected:
            out.append(row)
    return out


def _heifers_to_scan_query(farm_key: str):
    return (
        select(HerdInventory)
        .where(HerdInventory.farm == farm_key)
        .where(HerdInventory.category == "Youngstock")
        .where(cast(HerdInventory.rc, Integer).in_(HEIFERS_TO_SCAN_RC))
        .where(HerdInventory.dslh > HEIFERS_TO_SCAN_MIN_DSLH)
        .order_by(
            HerdInventory.pen.asc(),
            HerdInventory.cow_id.asc(),
            HerdInventory.dslh.desc(),
        )
    )


def _serialize_heifer_to_scan(row: HerdInventory) -> dict[str, Any]:
    return {
        "id": row.cow_id,
        "remark": (row.remark or "").strip() or None,
        "etag5": etag5(row.etag),
        "dslh": _whole_number(row.dslh),
        "tbrd": row.tbrd,
        "rpro": (row.rpro or "").strip() or None,
        "pen": (row.pen or "").strip() or None,
    }


def heifers_to_scan(
    db: Session, farm: str, pens: list[str] | None = None
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    rows = db.scalars(_heifers_to_scan_query(farm_key)).all()
    animals = [_serialize_heifer_to_scan(row) for row in rows]
    filtered = apply_pen_filter(animals, pens)
    return {
        "id": WIDGET_HEIFERS_TO_SCAN,
        "title": "Heifers To Scan",
        "count": len(animals),
        "filtered_count": len(filtered),
        "pens": unique_pens(animals),
        "rows": filtered,
        "farm": farm_key,
        "farm_label": FARM_LABELS[farm_key],
    }


def farm_reports(db: Session, farm: str) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    scan = heifers_to_scan(db, farm_key)
    return {
        "farm": farm_key,
        "farm_label": FARM_LABELS[farm_key],
        "widgets": [{"id": scan["id"], "title": scan["title"], "count": scan["count"]}],
        "heifers_to_scan": scan,
    }


def build_heifers_to_scan_xlsx(report: dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Heifers To Scan"
    headers = [label for _, label in TABLE_COLUMNS]
    ws.append(headers)
    for row in report.get("rows") or []:
        ws.append([_cell(row.get(key)) for key, _ in TABLE_COLUMNS])

    widths = [10, 24, 10, 8, 8, 10, 8]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    ws.page_setup.horizontalCentered = True

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_heifers_to_scan_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        SimpleDocTemplate,
        Table,
        TableStyle,
    )

    farm_label = report.get("farm_label") or ""
    title = f"Heifers To Scan · {farm_label}"
    generated = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    count = int(report.get("filtered_count") or len(report.get("rows") or []))

    buffer = io.BytesIO()
    page_w, page_h = A4
    left = right = 8 * mm
    top = 16 * mm
    bottom = 10 * mm
    usable_w = page_w - left - right
    raw_mm = (20, 72, 22, 18, 16, 24, 22)
    scale = usable_w / (sum(raw_mm) * mm)
    col_widths = [width * mm * scale for width in raw_mm]

    def _header(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(left, page_h - 11 * mm, title)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(left, page_h - 15 * mm, f"{count} animals  |  {generated}")
        canvas.drawRightString(page_w - right, page_h - 11 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        leftMargin=left,
        rightMargin=right,
        topMargin=top,
        bottomMargin=bottom,
    )

    header = [label for _, label in TABLE_COLUMNS]
    data_rows = [
        [_pdf_cell(key, row.get(key)) for key, _ in TABLE_COLUMNS]
        for row in report.get("rows") or []
    ]
    row_h = 6.2 * mm
    header_h = 6.8 * mm
    elements: list[Any] = []
    if not data_rows:
        empty = Table(
            [header, ["No animals match the selected pens."] + [""] * 6],
            colWidths=col_widths,
            repeatRows=1,
        )
        empty.setStyle(_pdf_table_style())
        empty.setStyle(
            TableStyle([("SPAN", (0, 1), (-1, 1)), ("ALIGN", (0, 1), (-1, 1), "LEFT")])
        )
        elements.append(empty)
    else:
        for start in range(0, len(data_rows), PDF_ROWS_PER_PAGE):
            chunk = data_rows[start : start + PDF_ROWS_PER_PAGE]
            table = Table(
                [header] + chunk,
                colWidths=col_widths,
                rowHeights=[header_h] + [row_h] * len(chunk),
                repeatRows=1,
            )
            table.setStyle(_pdf_table_style())
            elements.append(table)
            if start + PDF_ROWS_PER_PAGE < len(data_rows):
                elements.append(PageBreak())

    doc.build(elements, onFirstPage=_header, onLaterPages=_header)
    return buffer.getvalue()


def _pdf_cell(key: str, value: object) -> str:
    text = _cell(value)
    if key == "remark" and len(text) > 36:
        return text[:35] + "…"
    return text


def _pdf_table_style():
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d8dee4")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f9")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]
    )
