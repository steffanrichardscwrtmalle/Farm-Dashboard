"""Animals still to genomic-test (no result, no TSU submission)."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import GenomicResult, HerdInventory
from app.services.events_common import normalize_farms
from app.services.genomic_import import normalize_hbn
from app.services.herd_import_utils import BEEF_CBREED_MIN

FEMALE_GENDER = "Female"
DEFAULT_MIN_AGED = 60
DEFAULT_MAX_AGED = 999
GAD_MIN_BDAT = dt.date(2025, 9, 16)
_ZERO_GID_VALUES = {"", "0", "0.0", "-", "nan", "none"}


def etag4(raw: Any) -> str:
    """Last four digits of an ID, after stripping spaces."""
    digits = "".join(ch for ch in str(raw or "").replace(" ", "") if ch.isdigit())
    return digits[-4:] if digits else ""


def _is_blank_or_zero_gid(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if text.lower() in _ZERO_GID_VALUES:
        return True
    try:
        return float(text) == 0
    except ValueError:
        return False


def _is_blank_or_zero_date(value: dt.date | None) -> bool:
    if value is None:
        return True
    return value.year < 1900


def list_animals_to_test(
    db: Session,
    *,
    farms: list[str] | None = None,
    min_aged: int | None = DEFAULT_MIN_AGED,
    max_aged: int | None = DEFAULT_MAX_AGED,
) -> dict[str, Any]:
    """Heifers (lact = 0, CBRD < 102) with no genomic result and no TSU submission.

    GID, GTEST, and SUBD must all be blank or zero. Genomic results are matched
    on HBN = digits-only ETAG, the same way as Pending Results.
    Age is filtered to ``min_aged``–``max_aged`` days (defaults 60–999).
    GAD animals are limited to those born on or after 16 September 2025.
    """
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0}

    age_min = min_aged if min_aged is not None else DEFAULT_MIN_AGED
    age_max = max_aged if max_aged is not None else DEFAULT_MAX_AGED
    if age_max < age_min:
        return {"rows": [], "total": 0}

    genomic_hbns = set(db.scalars(select(GenomicResult.hbn)).all())

    query = (
        select(
            HerdInventory.farm,
            HerdInventory.cow_id,
            HerdInventory.etag,
            HerdInventory.aged,
            HerdInventory.pen,
            HerdInventory.gid,
            HerdInventory.gtest,
            HerdInventory.subd,
        )
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.gender == FEMALE_GENDER)
        .where(HerdInventory.lact == 0)
        .where(HerdInventory.cbrd.isnot(None))
        .where(HerdInventory.cbrd < BEEF_CBREED_MIN)
        .where(HerdInventory.aged.isnot(None))
        .where(HerdInventory.aged >= age_min)
        .where(HerdInventory.aged <= age_max)
        .where(
            or_(
                HerdInventory.farm != "GAD",
                and_(
                    HerdInventory.bdat.isnot(None),
                    HerdInventory.bdat >= GAD_MIN_BDAT,
                ),
            )
        )
    )

    rows: list[dict[str, Any]] = []
    for farm, cow_id, etag, aged, pen, gid, gtest, subd in db.execute(query).all():
        if not _is_blank_or_zero_gid(gid):
            continue
        if not _is_blank_or_zero_date(gtest):
            continue
        if not _is_blank_or_zero_date(subd):
            continue
        hbn = normalize_hbn(etag)
        if hbn and hbn in genomic_hbns:
            continue
        rows.append(
            {
                "id": (cow_id or "").strip(),
                "etag4": etag4(etag),
                "aged": aged,
                "pen": (pen or "").strip(),
                "farm": farm,
            }
        )

    rows.sort(
        key=lambda r: (
            int(r["etag4"]) if str(r["etag4"]).isdigit() else 10**9,
            r["farm"],
            r["id"],
        )
    )
    return {"rows": rows, "total": len(rows)}


def build_animals_to_test_csv(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "ETAG4", "AGED", "PEN", "Farm"])
    for row in rows:
        writer.writerow(
            [
                row.get("id", ""),
                row.get("etag4", ""),
                row.get("aged", ""),
                row.get("pen", ""),
                row.get("farm", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
