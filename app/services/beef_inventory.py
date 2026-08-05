"""Beef inventory report from herd_inventory, with optional JV filter."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, CowEvent, HerdInventory
from app.services.stock_valuations import animal_key

PDF_CONTENT_TYPE = "application/pdf"

JV_MODE_ALL = "all"
JV_MODE_EXCLUDE = "exclude"
JV_MODE_ONLY = "only"
JV_MODES = frozenset({JV_MODE_ALL, JV_MODE_EXCLUDE, JV_MODE_ONLY})
JvMode = Literal["all", "exclude", "only"]

_JV_EVENTS = ("GAME", "PATHWAY")


def _normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def normalize_jv_mode(jv_mode: str | None) -> JvMode:
    mode = (jv_mode or JV_MODE_ALL).strip().lower()
    if mode not in JV_MODES:
        raise ValueError("jv_mode must be all, exclude, or only")
    return mode  # type: ignore[return-value]


def _build_range_summary(grand_cm: int, grand_gad: int, age_month_count: int) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / age_month_count, 1) if age_month_count else 0.0

    grand_total = grand_cm + grand_gad
    return {
        "total": grand_total,
        "month_count": age_month_count,
        "average_per_month": avg(grand_total),
        "CM": {"total": grand_cm, "average_per_month": avg(grand_cm)},
        "GAD": {"total": grand_gad, "average_per_month": avg(grand_gad)},
    }


def _empty_range_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "month_count": 0,
        "average_per_month": 0,
        "CM": {"total": 0, "average_per_month": 0},
        "GAD": {"total": 0, "average_per_month": 0},
    }


def _jv_animal_keys(db: Session, farms: list[str]) -> set[tuple[str, str]]:
    """Animals with a GAME/PATHWAY event (still on farm if present in inventory)."""
    rows = db.execute(
        select(CowEvent.farm, CowEvent.etag, CowEvent.cow_id)
        .where(CowEvent.farm.in_(farms))
        .where(CowEvent.event.in_(_JV_EVENTS))
    ).all()
    return {animal_key(farm, etag, cow_id) for farm, etag, cow_id in rows}


def _jv_label(jv_mode: JvMode) -> str:
    if jv_mode == JV_MODE_EXCLUDE:
        return "No JV"
    if jv_mode == JV_MODE_ONLY:
        return "JV only"
    return "All (incl. JV)"


def get_beef_inventory_report(
    db: Session,
    farms: list[str] | None = None,
    min_age: int | None = None,
    max_age: int | None = None,
    jv_mode: str | None = JV_MODE_ALL,
) -> dict[str, Any]:
    selected_farms = _normalize_farms(farms)
    mode = normalize_jv_mode(jv_mode)

    # Age bounds from all on-farm beef (stable when toggling JV).
    bounds_row = db.execute(
        select(
            func.min(HerdInventory.months_old),
            func.max(HerdInventory.months_old),
        )
        .where(HerdInventory.category == "Beef")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old.isnot(None))
    ).one()

    data_min = int(bounds_row[0]) if bounds_row[0] is not None else 0
    data_max = int(bounds_row[1]) if bounds_row[1] is not None else 0

    effective_min = data_min if min_age is None else max(min_age, data_min)
    effective_max = data_max if max_age is None else min(max_age, data_max)

    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    latest_iso = latest_import.isoformat() if latest_import else None

    if effective_min > effective_max:
        return {
            "rows": [],
            "grand_total": {"CM": 0, "GAD": 0, "total": 0},
            "range_summary": _empty_range_summary(),
            "age_bounds": {"min": data_min, "max": data_max},
            "jv_mode": mode,
            "jv_label": _jv_label(mode),
            "latest_import": latest_iso,
        }

    animal_rows = db.execute(
        select(
            HerdInventory.months_old,
            HerdInventory.farm,
            HerdInventory.etag,
            HerdInventory.cow_id,
        )
        .where(HerdInventory.category == "Beef")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old >= effective_min)
        .where(HerdInventory.months_old <= effective_max)
    ).all()

    jv_keys: set[tuple[str, str]] | None = None
    if mode != JV_MODE_ALL:
        jv_keys = _jv_animal_keys(db, selected_farms)

    pivot: dict[int, dict[str, int]] = {}
    for months_old, farm, etag, cow_id in animal_rows:
        if jv_keys is not None:
            is_jv = animal_key(farm, etag, cow_id) in jv_keys
            if mode == JV_MODE_EXCLUDE and is_jv:
                continue
            if mode == JV_MODE_ONLY and not is_jv:
                continue
        age = int(months_old)
        pivot.setdefault(age, {"CM": 0, "GAD": 0})
        if farm in pivot[age]:
            pivot[age][farm] += 1

    rows: list[dict[str, Any]] = []
    grand_cm = 0
    grand_gad = 0
    for age in range(effective_min, effective_max + 1):
        cm = pivot.get(age, {}).get("CM", 0)
        gad = pivot.get(age, {}).get("GAD", 0)
        total = cm + gad
        rows.append({"months_old": age, "CM": cm, "GAD": gad, "total": total})
        grand_cm += cm
        grand_gad += gad

    age_month_count = effective_max - effective_min + 1
    return {
        "rows": rows,
        "grand_total": {
            "CM": grand_cm,
            "GAD": grand_gad,
            "total": grand_cm + grand_gad,
        },
        "range_summary": _build_range_summary(grand_cm, grand_gad, age_month_count),
        "age_bounds": {"min": data_min, "max": data_max},
        "jv_mode": mode,
        "jv_label": _jv_label(mode),
        "latest_import": latest_iso,
    }


def _export_columns(selected_farms: list[str]) -> list[str]:
    farms = [f for f in HERD_FARM_OPTIONS if f in selected_farms]
    return farms or list(HERD_FARM_OPTIONS)


def _format_import_note(latest_import: str | None) -> str:
    if not latest_import:
        return "No inventory import found"
    try:
        stamp = dt.datetime.fromisoformat(latest_import)
        return f"Latest import: {stamp.strftime('%d/%m/%Y %H:%M')}"
    except ValueError:
        return f"Latest import: {latest_import}"


def build_beef_inventory_csv(
    report: dict[str, Any], selected_farms: list[str]
) -> str:
    farms = _export_columns(selected_farms)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Months Old", *farms, "Total"])
    for row in report.get("rows", []):
        writer.writerow(
            [row["months_old"], *[row.get(f, 0) for f in farms], row.get("total", 0)]
        )
    grand = report.get("grand_total", {})
    writer.writerow(
        ["Grand Total", *[grand.get(f, 0) for f in farms], grand.get("total", 0)]
    )
    return buffer.getvalue()


def build_beef_inventory_pdf(
    report: dict[str, Any], selected_farms: list[str]
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    farms = _export_columns(selected_farms)
    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="Beef Inventory",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    bounds = report.get("age_bounds") or {}
    meta_bits = [
        f"Farms: {', '.join(farms)}",
        f"JV: {report.get('jv_label') or _jv_label(JV_MODE_ALL)}",
        f"Age range: {bounds.get('min', 0)}–{bounds.get('max', 0)} months",
        _format_import_note(report.get("latest_import")),
        f"Generated: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
    ]

    elements: list[Any] = [
        Paragraph("Beef Inventory", styles["Title"]),
        Paragraph(" &nbsp;|&nbsp; ".join(meta_bits), styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    header = ["Months Old", *farms, "Total"]
    table_data: list[list[Any]] = [header]
    for row in report.get("rows", []):
        table_data.append(
            [
                str(row["months_old"]),
                *[str(row.get(f, 0)) for f in farms],
                str(row.get("total", 0)),
            ]
        )
    grand = report.get("grand_total", {})
    table_data.append(
        ["Grand Total", *[str(grand.get(f, 0)) for f in farms], str(grand.get("total", 0))]
    )

    col_count = len(header)
    table = Table(table_data, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f3f6f9")]),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f6")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#9ca3af")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d8dee4")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(table)

    if col_count and not report.get("rows"):
        elements.append(Spacer(1, 6 * mm))
        elements.append(
            Paragraph("No beef cattle match the selected filters.", styles["Italic"])
        )

    doc.build(elements)
    return buffer.getvalue()
