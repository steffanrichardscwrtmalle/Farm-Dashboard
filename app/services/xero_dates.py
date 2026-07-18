"""Shared Xero date/datetime parsing helpers."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

_MS_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")


def parse_xero_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    match = _MS_DATE_RE.search(text)
    if match:
        millis = int(match.group(1))
        return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC).date()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_xero_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    match = _MS_DATE_RE.search(text)
    if match:
        millis = int(match.group(1))
        return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC).replace(tzinfo=None)
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
