"""Home-dashboard unique-cow hoof trimming widgets (7 days before today)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS
from app.services.events_common import _build_footrim_throughput

HOOF_TRIM_WIDGET_DAYS = 7


def get_hoof_trimming_7d(
    db: Session,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Unique FOOTRIM/LAME cows per farm for yesterday and the 6 days before.

    A cow with both events on the same day counts once. Today is excluded.
    """
    today = as_of or dt.date.today()
    effective_to = today - dt.timedelta(days=1)
    effective_from = today - dt.timedelta(days=HOOF_TRIM_WIDGET_DAYS)
    farms = list(HERD_FARM_OPTIONS)
    throughput = _build_footrim_throughput(
        db,
        selected_farms=farms,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    summary = throughput.get("summary") or {}
    return {
        "from": effective_from.isoformat(),
        "to": effective_to.isoformat(),
        "as_of": today.isoformat(),
        "href": "/events/hooftrimming",
        "label": "Hoof — 7 Days",
        "farms": [
            {
                "farm": farm,
                "count": int((summary.get(farm) or {}).get("unique_cows") or 0),
                "href": "/events/hooftrimming",
            }
            for farm in farms
        ],
    }
