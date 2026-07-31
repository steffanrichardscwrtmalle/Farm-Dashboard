"""Cross-reference inventory SREG against genomic Sire Reg to find conflicts."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GenomicResult, HerdInventory
from app.services.events_common import normalize_farms
from app.services.genomic_import import normalize_hbn


def _last12_digits(value: Any) -> str | None:
    """Return a comparable digit key for a sire registration.

    Keeps the last 12 digits (ignoring letters / spaces), then strips leading
    zeros so genomic ``003244007413`` matches inventory ``3244007413``.
    """
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    key = digits[-12:].lstrip("0")
    return key or "0"


def list_sire_conflicts(
    db: Session,
    *,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    """Animals present in both inventory and genomic results whose SREG disagree.

    Comparison uses the last 12 digits of each registration (so differing country
    prefixes / lengths still match) and ignores leading zeros. Only animals where
    both sides have a value and the normalised keys differ are reported as conflicts.
    """
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "count": 0}

    genomic_by_hbn = {
        row.hbn: row for row in db.scalars(select(GenomicResult)).all()
    }

    rows: list[dict[str, Any]] = []
    inventory_rows = db.execute(
        select(
            HerdInventory.farm,
            HerdInventory.cow_id,
            HerdInventory.etag,
            HerdInventory.sreg,
        ).where(HerdInventory.farm.in_(selected_farms))
    ).all()

    for farm, cow_id, etag, sreg in inventory_rows:
        if not etag:
            continue
        hbn = normalize_hbn(etag)
        if not hbn:
            continue
        genomic = genomic_by_hbn.get(hbn)
        if genomic is None:
            continue

        inv_key = _last12_digits(sreg)
        gen_key = _last12_digits(genomic.sire_reg)
        if inv_key is None or gen_key is None:
            continue
        if inv_key == gen_key:
            continue

        rows.append(
            {
                "id": (cow_id or "").strip(),
                "etag": (etag or "").strip(),
                "sreg": inv_key,
                "genomic_sreg": gen_key,
                "farm": farm,
            }
        )

    rows.sort(key=lambda r: (r["farm"], r["id"]))
    return {"rows": rows, "count": len(rows)}


def build_sire_conflicts_csv(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "ETAG", "SREG (Inventory)", "SREG (Genomic)", "Farm"])
    for row in rows:
        writer.writerow(
            [
                row.get("id", ""),
                row.get("etag", ""),
                row.get("sreg", ""),
                row.get("genomic_sreg", ""),
                row.get("farm", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
