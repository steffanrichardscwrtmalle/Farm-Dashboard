"""Home-dashboard dairy semen usage widget (rolling 30 days before today)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, CowEvent
from app.services.breeding_sires import classify_semen_type, load_sire_overrides

HealthLevel = Literal["red", "yellow", "green", "purple"]


def _status_for_count(count: int) -> HealthLevel:
    if count <= 370:
        return "red"
    if count <= 390:
        return "yellow"
    if count <= 420:
        return "green"
    return "purple"


def get_dairy_semen_30d(
    db: Session,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Dairy BRED totals for CM+GAD.

    - ``count``: 30 days before today (``as_of - 30`` .. ``as_of - 1``)
    - ``avg_30d_120``: dairy count over 120 days before today, divided by 4
    """
    today = as_of or dt.date.today()
    effective_to = today - dt.timedelta(days=1)
    from_30 = today - dt.timedelta(days=30)
    from_120 = today - dt.timedelta(days=120)
    farms = list(HERD_FARM_OPTIONS)
    overrides = load_sire_overrides(db)

    # One query for the longer window; split into 30d vs full 120d in Python.
    rows = db.execute(
        select(CowEvent.remark, CowEvent.event_date)
        .where(CowEvent.event == "BRED")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(farms))
        .where(CowEvent.event_date >= from_120)
        .where(CowEvent.event_date <= effective_to)
    ).all()

    count_30 = 0
    count_120 = 0
    for remark, event_date in rows:
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        if classify_semen_type(remark, overrides) != "dairy":
            continue
        count_120 += 1
        if event_date >= from_30:
            count_30 += 1

    avg_30d_120 = int(round(count_120 / 4))
    status = _status_for_count(count_30)
    avg_status = _status_for_count(avg_30d_120)
    return {
        "count": count_30,
        "avg_30d_120": avg_30d_120,
        "count_120": count_120,
        "status": status,
        "avg_status": avg_status,
        "from": from_30.isoformat(),
        "to": effective_to.isoformat(),
        "from_120": from_120.isoformat(),
        "as_of": today.isoformat(),
        "href": "/events/breedings",
        "label": "Dairy Semen - 30 Days",
    }
