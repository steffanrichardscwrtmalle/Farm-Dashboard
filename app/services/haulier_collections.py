"""Query milk collections, joined to NML quality, for the dashboard page.

Each collection (one tanker load) is matched to its NML milk-quality sample by
normalised sample number and collection date (allowing +/- 1 day, since the lab
and haulier can disagree by a day).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MilkCollection, NmlMilkResult
from app.services.events_common import normalize_farms

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

_HEADERS = (
    "Farm",
    "Date",
    "Sample",
    "Driver",
    "Vehicle",
    "Arrival",
    "Depart",
    "Volume (L)",
    "Temp (°C)",
    "Temp detail",
    "Butterfat %",
    "Protein %",
    "SCC",
    "BactoScan",
    "Urea %",
    "A/B",
)

# Quality fields copied onto a matched collection row.
_NML_FIELDS = ("butterfat_pct", "protein_pct", "scc", "bactoscan", "urea_pct")


def _norm_sample(value: str | None) -> str:
    s = (value or "").strip().lstrip("0")
    return s or "0"


def _ab_label(value: bool | None) -> str:
    if value is True:
        return "Pass"
    if value is False:
        return "Fail"
    return ""


def _time_str(value: dt.time | None) -> str:
    return value.strftime("%H:%M") if value else ""


def _build_nml_index(
    rows: list[NmlMilkResult],
) -> dict[tuple[str, str], list[NmlMilkResult]]:
    index: dict[tuple[str, str], list[NmlMilkResult]] = {}
    for row in rows:
        if not row.farm or not row.sample_date:
            continue
        index.setdefault((row.farm, _norm_sample(row.sample_id)), []).append(row)
    return index


def _match_nml(
    collection: MilkCollection,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> NmlMilkResult | None:
    candidates = index.get((collection.farm or "", _norm_sample(collection.sample_id)))
    if not candidates:
        return None
    best: NmlMilkResult | None = None
    best_distance = 2  # only accept +/- 1 day
    for cand in candidates:
        distance = abs((cand.sample_date - collection.collection_date).days)
        if distance < best_distance:
            best = cand
            best_distance = distance
    return best


def _row_to_dict(
    collection: MilkCollection, nml: NmlMilkResult | None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "farm": collection.farm or "",
        "collection_date": collection.collection_date.isoformat()
        if collection.collection_date
        else "",
        "sample_id": collection.sample_id or "",
        "driver": collection.driver or "",
        "vehicle_reg": collection.vehicle_reg or "",
        "arrival_time": _time_str(collection.arrival_time),
        "depart_time": _time_str(collection.depart_time),
        "volume_litres": collection.volume_litres,
        "temp_c": collection.temp_c,
        "temp_raw": collection.temp_raw or "",
        "matched": nml is not None,
        "antibiotic_pass": nml.antibiotic_pass if nml else None,
    }
    for field in _NML_FIELDS:
        row[field] = getattr(nml, field) if nml else None
    return row


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(metric: str, dp: int = 2) -> float | None:
        values = [float(r[metric]) for r in rows if r.get(metric) is not None]
        return round(sum(values) / len(values), dp) if values else None

    volumes = [r["volume_litres"] for r in rows if r.get("volume_litres") is not None]
    latest = max((r["collection_date"] for r in rows if r["collection_date"]), default="")
    total_volume = sum(volumes) if volumes else 0
    collection_days = {
        r["collection_date"]
        for r in rows
        if r.get("collection_date") and r.get("volume_litres") is not None
    }
    avg_daily_volume = round(total_volume / len(collection_days)) if collection_days else None
    return {
        "count": len(rows),
        "latest_collection_date": latest,
        "total_volume": total_volume,
        "avg_volume": round(sum(volumes) / len(volumes)) if volumes else None,
        "avg_daily_volume": avg_daily_volume,
        "days": len(collection_days),
        "avg_temp": avg("temp_c", 2),
        "matched_count": sum(1 for r in rows if r.get("matched")),
        "unmatched_count": sum(1 for r in rows if not r.get("matched")),
        "avg_butterfat_pct": avg("butterfat_pct"),
        "avg_protein_pct": avg("protein_pct"),
    }


# Per-day trend metrics: volume is summed, the rest averaged.
_TREND_SUM = ("volume_litres",)
_TREND_AVG = ("temp_c", "butterfat_pct", "protein_pct", "scc", "bactoscan", "urea_pct")


def _build_trend(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    for row in rows:
        farm = row["farm"] or "?"
        date = row["collection_date"]
        if not date:
            continue
        bucket = buckets.setdefault(
            (farm, date), {m: [] for m in (*_TREND_SUM, *_TREND_AVG)}
        )
        for metric in (*_TREND_SUM, *_TREND_AVG):
            value = row.get(metric)
            if value is not None:
                bucket[metric].append(float(value))

    trend: dict[str, list[dict[str, Any]]] = {}
    for (farm, date), metrics in buckets.items():
        point: dict[str, Any] = {"date": date}
        for metric in _TREND_SUM:
            values = metrics[metric]
            point[metric] = round(sum(values)) if values else None
        for metric in _TREND_AVG:
            values = metrics[metric]
            point[metric] = round(sum(values) / len(values), 3) if values else None
        trend.setdefault(farm, []).append(point)
    for points in trend.values():
        points.sort(key=lambda item: item["date"])
    return trend


def list_collections(
    db: Session,
    *,
    farms: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "summary": _summary([]), "trend": {}}

    query = select(MilkCollection).where(MilkCollection.farm.in_(selected_farms))
    if date_from is not None:
        query = query.where(MilkCollection.collection_date >= date_from)
    if date_to is not None:
        query = query.where(MilkCollection.collection_date <= date_to)
    query = query.order_by(
        MilkCollection.collection_date.desc(), MilkCollection.sample_id.desc()
    )
    collections = db.scalars(query).all()

    # Pull NML rows for the same farms within a +/- 1 day buffer for matching.
    nml_query = select(NmlMilkResult).where(NmlMilkResult.farm.in_(selected_farms))
    if date_from is not None:
        nml_query = nml_query.where(
            NmlMilkResult.sample_date >= date_from - dt.timedelta(days=1)
        )
    if date_to is not None:
        nml_query = nml_query.where(
            NmlMilkResult.sample_date <= date_to + dt.timedelta(days=1)
        )
    index = _build_nml_index(list(db.scalars(nml_query).all()))

    rows = [_row_to_dict(c, _match_nml(c, index)) for c in collections]
    return {
        "rows": rows,
        "total": len(rows),
        "summary": _summary(rows),
        "trend": _build_trend(rows),
    }


def _export_cells(row: dict[str, Any]) -> list[Any]:
    return [
        row.get("farm", ""),
        row.get("collection_date", ""),
        row.get("sample_id", ""),
        row.get("driver", ""),
        row.get("vehicle_reg", ""),
        row.get("arrival_time", ""),
        row.get("depart_time", ""),
        row.get("volume_litres"),
        row.get("temp_c"),
        row.get("temp_raw", ""),
        row.get("butterfat_pct"),
        row.get("protein_pct"),
        row.get("scc"),
        row.get("bactoscan"),
        row.get("urea_pct"),
        _ab_label(row.get("antibiotic_pass")),
    ]


def build_collections_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_HEADERS)
    for row in rows:
        writer.writerow(_export_cells(row))
    return buffer.getvalue()


def build_collections_xlsx(rows: list[dict[str, Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Milk Collections"
    ws.append(list(_HEADERS))
    for row in rows:
        ws.append(_export_cells(row))

    widths = [8, 12, 10, 16, 12, 9, 9, 11, 10, 14, 11, 10, 8, 11, 8, 7]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
