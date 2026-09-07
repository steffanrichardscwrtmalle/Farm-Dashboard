"""Fetch feed ration data from Feedlync via the public HTTP API (no browser)."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import (
    FEEDLYNC_API_BASE,
    FEEDLYNC_CLIENT_ID,
    FEEDLYNC_FARM_ID,
    FEEDLYNC_TOKEN_SCOPE,
    FEEDLYNC_TOKEN_URL,
)
from app.services.feedlync_auth import (
    FeedlyncAuthError,
    resolve_refresh_token,
    save_refresh_token,
)
from app.services.feedlync_ro_login import (
    auto_login_enabled,
    fetch_refresh_token_via_login,
)

_RATION_DETAIL_PARAMS = {
    "feedplans": "true",
    "ingredients": "true",
    "nutrients": "false",
    "ingredientNutrients": "false",
}

# Loaded Mixes → By Ingredient. Do not use loads/fedmixes (different totals).
_LOADED_MIX_INGREDIENT_PATH = "loads/ingredientspends"


def _refresh_access_token(
    client: httpx.Client,
    refresh_token: str,
) -> tuple[str, str | None]:
    response = client.post(
        FEEDLYNC_TOKEN_URL,
        data={
            "client_id": FEEDLYNC_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": FEEDLYNC_TOKEN_SCOPE,
            "client_info": "1",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code in (400, 401):
        raise FeedlyncAuthError(
            "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
        )
    response.raise_for_status()
    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise FeedlyncAuthError("Feedlync token response did not include access_token")
    new_refresh = payload.get("refresh_token")
    return str(access_token), str(new_refresh) if new_refresh else None


def _login_and_store(db: Session) -> str:
    """Run the unattended login and persist the resulting refresh token."""
    refresh_token = fetch_refresh_token_via_login()
    save_refresh_token(db, refresh_token)
    return refresh_token


def _acquire_access_token(db: Session, client: httpx.Client) -> str:
    """Return a valid access token, using auto-login when needed/possible.

    Handles two cases transparently: no stored token yet, and a stored token
    that has expired (FeedLync SPA refresh tokens last ~24h). Falls back to the
    manual reconnect error when auto-login is not configured.
    """
    try:
        refresh_token = resolve_refresh_token(db)
    except FeedlyncAuthError:
        if not auto_login_enabled():
            raise
        refresh_token = _login_and_store(db)

    try:
        access_token, new_refresh = _refresh_access_token(client, refresh_token)
    except FeedlyncAuthError:
        if not auto_login_enabled():
            raise
        refresh_token = _login_and_store(db)
        access_token, new_refresh = _refresh_access_token(client, refresh_token)

    if new_refresh:
        save_refresh_token(db, new_refresh)
    return access_token


def _api_headers(access_token: str, farm_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "farm": farm_id,
        "accept": "application/json",
    }


def _extract_farm_ids(summary: Any) -> list[str]:
    if FEEDLYNC_FARM_ID:
        return [part.strip() for part in FEEDLYNC_FARM_ID.split(",") if part.strip()]

    farms: list[Any]
    if isinstance(summary, list):
        farms = summary
    elif isinstance(summary, dict):
        farms = summary.get("farms") or summary.get("items") or []
    else:
        farms = []

    farm_ids = [str(farm["id"]) for farm in farms if isinstance(farm, dict) and farm.get("id")]
    if not farm_ids:
        raise ValueError("No farms found from Feedlync /farms/summary; set FEEDLYNC_FARM_ID")
    return farm_ids


def _recipe_totals(ration_ingredients: list[dict[str, Any]]) -> tuple[float, float, float]:
    fresh_per_cow = 0.0
    dm_per_cow = 0.0
    cost_per_cow = 0.0

    for row in ration_ingredients:
        amount = float(row.get("amountField") or 0)
        ingredient = row.get("ingredient") or {}
        drymatter = float(ingredient.get("drymatter") or 0)
        price = float(ingredient.get("price") or 0)

        fresh_per_cow += amount
        dm_per_cow += amount * drymatter / 100.0
        cost_per_cow += amount * price / 1000.0

    return fresh_per_cow, dm_per_cow, cost_per_cow


def _ration_to_rows(ration: dict[str, Any], *, scraped_date: dt.date) -> list[dict[str, Any]]:
    if ration.get("isHidden"):
        return []

    fresh_per_cow, dm_per_cow_base, cost_per_cow_base = _recipe_totals(
        ration.get("rationIngredients") or []
    )
    rows: list[dict[str, Any]] = []

    for feed_plan in ration.get("feedPlans") or []:
        if feed_plan.get("isHidden"):
            continue

        for feed_plan_pen in feed_plan.get("feedPlanPens") or []:
            pen = feed_plan_pen.get("pen") or {}
            group_name = (pen.get("name") or "").strip()
            if not group_name or group_name == "N/A":
                continue

            factor = float(feed_plan_pen.get("rationsfactor") or 0)
            if factor == 0:
                continue

            cow_count = pen.get("numberOfAnimals")
            cows = float(cow_count) if cow_count is not None else 0.0

            dm_kg_per_cow = dm_per_cow_base * factor
            rows.append(
                {
                    "ration_name": ration.get("name") or "",
                    "group_name": group_name,
                    "cow_count": cow_count,
                    "feed_percent": round(factor * 100, 4),
                    "total_fresh": round(fresh_per_cow * factor * cows),
                    "total_dm": round(dm_kg_per_cow * cows, 2),
                    "dm_kg_per_cow": round(dm_kg_per_cow, 2),
                    "cost": round(cost_per_cow_base * factor * cows, 2),
                    "scraped_date": scraped_date,
                }
            )

    return rows


def _fetch_ration_detail(
    client: httpx.Client,
    *,
    access_token: str,
    farm_id: str,
    ration_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{FEEDLYNC_API_BASE}/rations/{ration_id}",
        headers=_api_headers(access_token, farm_id),
        params=_RATION_DETAIL_PARAMS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected ration detail payload for {ration_id}")
    return payload


def _fetch_farm_rows(
    client: httpx.Client,
    *,
    access_token: str,
    farm_id: str,
    scraped_date: dt.date,
    ration_name: str | None = None,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{FEEDLYNC_API_BASE}/rations",
        headers=_api_headers(access_token, farm_id),
    )
    response.raise_for_status()
    rations = response.json()
    if not isinstance(rations, list):
        raise ValueError("Unexpected Feedlync /rations response")

    if ration_name and ration_name.lower() != "all":
        rations = [
            ration
            for ration in rations
            if isinstance(ration, dict)
            and ration_name.lower() in (ration.get("name") or "").lower()
        ]
        if not rations:
            raise ValueError(f"Ration '{ration_name}' not found on Feedlync")

    rows: list[dict[str, Any]] = []
    for ration_summary in rations:
        if not isinstance(ration_summary, dict):
            continue
        ration_id = ration_summary.get("id")
        if not ration_id:
            continue

        ration = _fetch_ration_detail(
            client,
            access_token=access_token,
            farm_id=farm_id,
            ration_id=str(ration_id),
        )
        rows.extend(_ration_to_rows(ration, scraped_date=scraped_date))

    return rows


def fetch_feed_data(db: Session, *, ration_name: str | None = None) -> list[dict[str, Any]]:
    """
    Fetch all feed-plan pen rows from Feedlync for configured farm(s).

    Returns the same row dict shape used by feed_rate_import.
    """
    scraped_date = dt.date.today()
    all_rows: list[dict[str, Any]] = []

    with httpx.Client(timeout=60.0) as client:
        access_token = _acquire_access_token(db, client)

        summary_response = client.get(
            f"{FEEDLYNC_API_BASE}/farms/summary",
            headers={"Authorization": f"Bearer {access_token}", "accept": "application/json"},
        )
        if summary_response.status_code in (401, 403):
            raise FeedlyncAuthError(
                "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
            )
        summary_response.raise_for_status()
        farm_ids = _extract_farm_ids(summary_response.json())

        for farm_id in farm_ids:
            all_rows.extend(
                _fetch_farm_rows(
                    client,
                    access_token=access_token,
                    farm_id=farm_id,
                    scraped_date=scraped_date,
                    ration_name=ration_name,
                )
            )

    return all_rows


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    """Return the first and last calendar day of a month."""
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last)


def previous_calendar_month(today: dt.date | None = None) -> dt.date:
    """Return the first day of the previous calendar month."""
    current = today or dt.date.today()
    first_of_this_month = current.replace(day=1)
    previous = first_of_this_month - dt.timedelta(days=1)
    return previous.replace(day=1)


def _utc_month_range(period_start: dt.date, period_end: dt.date) -> tuple[str, str]:
    """Feedlync Last Month window as UTC midnight → next-day midnight (exclusive)."""
    start = dt.datetime(
        period_start.year, period_start.month, period_start.day, tzinfo=dt.timezone.utc
    )
    end_exclusive = dt.datetime(
        period_end.year, period_end.month, period_end.day, tzinfo=dt.timezone.utc
    ) + dt.timedelta(days=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end_exclusive.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


def _as_float(value: Any) -> float:
    if value is None or value is False:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ingredient_name(row: dict[str, Any]) -> str:
    nested = row.get("ingredient")
    if isinstance(nested, dict):
        name = nested.get("name") or nested.get("ingredientName")
        if name:
            return str(name).strip()
    for key in ("ingredientName", "name", "ingredient"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _as_fed_kg(row: dict[str, Any]) -> float:
    for key in (
        "quantity",
        "asFed",
        "asFedQuantity",
        "fresh",
        "freshQuantity",
        "loadedQuantity",
        "loaded",
        "weight",
    ):
        if key in row and row[key] is not None:
            return _as_float(row[key])
    return 0.0


def _dm_kg(row: dict[str, Any]) -> float:
    for key in (
        "drymatterQuantity",
        "dryMatterQuantity",
        "dmQuantity",
        "dmWeight",
        "drymatter",
        "dryMatter",
    ):
        if key in row and row[key] is not None:
            return _as_float(row[key])
    return 0.0


def _spend_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "ingredientSpends",
        "ingredients",
        "items",
        "data",
        "results",
        "rows",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _parse_ingredient_spends(payload: Any) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, float | str]] = {}
    for item in _spend_items(payload):
        name = _ingredient_name(item)
        if not name:
            continue
        current = merged.setdefault(
            name,
            {"ingredient_name": name, "as_fed_kg": 0.0, "dm_kg": 0.0, "cost": 0.0},
        )
        current["as_fed_kg"] = float(current["as_fed_kg"]) + _as_fed_kg(item)
        current["dm_kg"] = float(current["dm_kg"]) + _dm_kg(item)
        current["cost"] = float(current["cost"]) + _as_float(item.get("cost"))
    rows = list(merged.values())
    rows.sort(key=lambda row: float(row["as_fed_kg"]), reverse=True)
    return rows


def _authenticate_farms(
    db: Session, client: httpx.Client
) -> tuple[str, list[str]]:
    access_token = _acquire_access_token(db, client)
    summary_response = client.get(
        f"{FEEDLYNC_API_BASE}/farms/summary",
        headers={"Authorization": f"Bearer {access_token}", "accept": "application/json"},
    )
    if summary_response.status_code in (401, 403):
        raise FeedlyncAuthError(
            "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
        )
    summary_response.raise_for_status()
    return access_token, _extract_farm_ids(summary_response.json())


def fetch_loaded_mix_ingredient_usage(
    db: Session,
    *,
    period_start: dt.date,
    period_end: dt.date,
) -> list[dict[str, Any]]:
    """
    Fetch Loaded Mixes → By Ingredient totals for a calendar date range.

    Uses loads/ingredientspends (not loads/fedmixes). Weights are kilograms.
    """
    from_utc, to_utc = _utc_month_range(period_start, period_end)
    param_candidates = (
        {"from": from_utc, "to": to_utc},
        {"startDate": from_utc, "endDate": to_utc},
        {"fromDate": from_utc, "toDate": to_utc},
    )
    merged: dict[str, dict[str, float | str]] = {}

    with httpx.Client(timeout=120.0) as client:
        access_token, farm_ids = _authenticate_farms(db, client)
        for farm_id in farm_ids:
            response = None
            last_error: str | None = None
            for params in param_candidates:
                response = client.get(
                    f"{FEEDLYNC_API_BASE}/{_LOADED_MIX_INGREDIENT_PATH}",
                    headers=_api_headers(access_token, farm_id),
                    params=params,
                )
                if response.status_code in (401, 403):
                    raise FeedlyncAuthError(
                        "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
                    )
                if response.status_code == 200:
                    break
                last_error = f"{response.status_code} {response.text[:300]}"
            if response is None or response.status_code != 200:
                raise ValueError(
                    "Feedlync Loaded Mixes request failed"
                    + (f": {last_error}" if last_error else "")
                )
            for row in _parse_ingredient_spends(response.json()):
                name = str(row["ingredient_name"])
                current = merged.setdefault(
                    name,
                    {
                        "ingredient_name": name,
                        "as_fed_kg": 0.0,
                        "dm_kg": 0.0,
                        "cost": 0.0,
                    },
                )
                current["as_fed_kg"] = float(current["as_fed_kg"]) + float(row["as_fed_kg"])
                current["dm_kg"] = float(current["dm_kg"]) + float(row["dm_kg"])
                current["cost"] = float(current["cost"]) + float(row["cost"])

    rows = list(merged.values())
    rows.sort(key=lambda row: float(row["as_fed_kg"]), reverse=True)
    return rows
