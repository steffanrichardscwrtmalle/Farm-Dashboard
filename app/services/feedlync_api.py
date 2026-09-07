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

# Default assignments used only to seed the settings table on first run.
USAGE_RATIONS_BY_FARM: dict[str, tuple[str, ...]] = {
    "GAD": (
        "Coomb Bulling Heifer Premix",
        "Coomb Close Up Premix",
        "Coomb Far Off",
        "Coomb Milker Premix",
        "Coomb Pregnant Heifers",
    ),
    "CM": (
        "Cwrt Bulling Heifer",
        "Cwrt Close Up Ration",
        "Cwrt Far Off Ration",
        "Cwrt Milkers",
        "Cwrt Pregnant Heifers",
    ),
}
DEFAULT_USAGE_RATION_FARM_LOOKUP = {
    name.casefold(): farm
    for farm, names in USAGE_RATIONS_BY_FARM.items()
    for name in names
}

# Feedlync Loaded Mixes "Select Ingredient" types. Forage is id 1 in the live catalog.
_FORAGE_INGREDIENT_TYPE_ID = 1
USAGE_EXCLUDED_INGREDIENT_TYPE_NAMES = frozenset({"forage"})


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


def usage_ingredient_key(name: str) -> str:
    return " ".join((name or "").strip().casefold().split())


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


def farm_for_usage_ration(
    ration_name: str,
    lookup: dict[str, str] | None = None,
) -> str | None:
    table = lookup if lookup is not None else DEFAULT_USAGE_RATION_FARM_LOOKUP
    farm = table.get((ration_name or "").strip().casefold())
    if farm in {"CM", "GAD"}:
        return farm
    return None


def _is_forage_ingredient(
    item: dict[str, Any],
    forage_type_ids: set[int] | None = None,
) -> bool:
    """True when Feedlync Select Ingredient type is Forage."""
    ids = forage_type_ids if forage_type_ids is not None else {_FORAGE_INGREDIENT_TYPE_ID}
    raw_id = item.get("ingredientTypeId")
    if raw_id is not None:
        try:
            if int(raw_id) in ids:
                return True
        except (TypeError, ValueError):
            pass
    type_name = str(item.get("ingredientTypeName") or "").strip().casefold()
    return type_name in USAGE_EXCLUDED_INGREDIENT_TYPE_NAMES


def _usage_ingredient_included(
    item: dict[str, Any],
    *,
    inclusion: dict[str, bool] | None = None,
    forage_type_ids: set[int] | None = None,
) -> bool:
    name = usage_ingredient_key(_ingredient_name(item))
    if inclusion and name in inclusion:
        return bool(inclusion[name])
    return not _is_forage_ingredient(item, forage_type_ids)


def _forage_ingredient_type_ids(
    client: httpx.Client, headers: dict[str, str]
) -> set[int]:
    response = client.get(f"{FEEDLYNC_API_BASE}/ingredienttypes", headers=headers)
    if response.status_code in (401, 403):
        raise FeedlyncAuthError(
            "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
        )
    ids: set[int] = set()
    if response.status_code == 200:
        payload = response.json()
        items = payload if isinstance(payload, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().casefold()
            if name not in USAGE_EXCLUDED_INGREDIENT_TYPE_NAMES:
                continue
            raw_id = item.get("id")
            if raw_id is None:
                continue
            try:
                ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
    return ids or {_FORAGE_INGREDIENT_TYPE_ID}


def _parse_ingredient_spends_by_farm(
    payload: Any,
    *,
    forage_type_ids: set[int] | None = None,
    ration_lookup: dict[str, str] | None = None,
    ingredient_inclusion: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Split Loaded Mixes ingredient totals using the nested ration breakdown."""
    merged: dict[tuple[str, str], dict[str, float | str]] = {}
    for item in _spend_items(payload):
        ingredient = _ingredient_name(item)
        if not ingredient:
            continue
        if not _usage_ingredient_included(
            item,
            inclusion=ingredient_inclusion,
            forage_type_ids=forage_type_ids,
        ):
            continue
        for ration in item.get("rations") or []:
            if not isinstance(ration, dict):
                continue
            farm = farm_for_usage_ration(
                str(ration.get("rationName") or ""), lookup=ration_lookup
            )
            if not farm:
                continue
            current = merged.setdefault(
                (farm, ingredient),
                {
                    "farm": farm,
                    "ingredient_name": ingredient,
                    "as_fed_kg": 0.0,
                    "dm_kg": 0.0,
                    "cost": 0.0,
                },
            )
            current["as_fed_kg"] = float(current["as_fed_kg"]) + _as_float(
                ration.get("quantity")
            )
            current["dm_kg"] = float(current["dm_kg"]) + _as_float(
                ration.get("drymatterQuantity")
            )
            current["cost"] = float(current["cost"]) + _as_float(ration.get("cost"))
    rows = list(merged.values())
    rows.sort(
        key=lambda row: (
            str(row["farm"]),
            -float(row["as_fed_kg"]),
            str(row["ingredient_name"]),
        )
    )
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


def fetch_ration_summaries(db: Session) -> list[dict[str, str]]:
    """Return unique Feedlync ration names (no recipe detail)."""
    merged: dict[str, dict[str, str]] = {}
    with httpx.Client(timeout=60.0) as client:
        access_token, farm_ids = _authenticate_farms(db, client)
        for farm_id in farm_ids:
            response = client.get(
                f"{FEEDLYNC_API_BASE}/rations",
                headers=_api_headers(access_token, farm_id),
            )
            if response.status_code in (401, 403):
                raise FeedlyncAuthError(
                    "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
                )
            response.raise_for_status()
            rations = response.json()
            if not isinstance(rations, list):
                raise ValueError("Unexpected Feedlync /rations response")
            for ration in rations:
                if not isinstance(ration, dict):
                    continue
                name = str(ration.get("name") or "").strip()
                if not name:
                    continue
                merged[name.casefold()] = {
                    "name": name,
                    "id": str(ration.get("id") or ""),
                }
    return sorted(merged.values(), key=lambda row: row["name"].casefold())


def _ingredient_type_catalog(
    client: httpx.Client, headers: dict[str, str]
) -> dict[int, str]:
    response = client.get(f"{FEEDLYNC_API_BASE}/ingredienttypes", headers=headers)
    if response.status_code in (401, 403):
        raise FeedlyncAuthError(
            "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
        )
    names: dict[int, str] = {}
    if response.status_code != 200:
        return names
    payload = response.json()
    items = payload if isinstance(payload, list) else []
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            type_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        names[type_id] = str(item.get("name") or "").strip()
    return names


def fetch_ingredient_summaries(db: Session) -> list[dict[str, Any]]:
    """Return unique Feedlync ingredients with Select Ingredient type."""
    merged: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=60.0) as client:
        access_token, farm_ids = _authenticate_farms(db, client)
        type_names = _ingredient_type_catalog(
            client, _api_headers(access_token, farm_ids[0])
        )
        forage_ids = {
            type_id
            for type_id, name in type_names.items()
            if name.casefold() in USAGE_EXCLUDED_INGREDIENT_TYPE_NAMES
        } or {_FORAGE_INGREDIENT_TYPE_ID}
        for farm_id in farm_ids:
            response = client.get(
                f"{FEEDLYNC_API_BASE}/ingredients",
                headers=_api_headers(access_token, farm_id),
            )
            if response.status_code in (401, 403):
                raise FeedlyncAuthError(
                    "FeedLync session expired. Use Reconnect FeedLync on the Feed Rate page."
                )
            response.raise_for_status()
            ingredients = response.json()
            if not isinstance(ingredients, list):
                raise ValueError("Unexpected Feedlync /ingredients response")
            for item in ingredients:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                raw_type = item.get("ingredientTypeId")
                type_id: int | None
                try:
                    type_id = int(raw_type) if raw_type is not None else None
                except (TypeError, ValueError):
                    type_id = None
                type_name = str(item.get("ingredientTypeName") or "").strip()
                if not type_name and type_id is not None:
                    type_name = type_names.get(type_id, "")
                merged[name.casefold()] = {
                    "name": name,
                    "id": str(item.get("id") or ""),
                    "ingredient_type_id": type_id,
                    "ingredient_type_name": type_name,
                    "is_forage": (
                        type_name.casefold() in USAGE_EXCLUDED_INGREDIENT_TYPE_NAMES
                        or type_id in forage_ids
                    ),
                }
    return sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("ingredient_type_name") or "").casefold(),
            str(row["name"]).casefold(),
        ),
    )


def fetch_loaded_mix_ingredient_usage(
    db: Session,
    *,
    period_start: dt.date,
    period_end: dt.date,
) -> list[dict[str, Any]]:
    """
    Fetch Loaded Mixes → By Ingredient totals for a calendar date range.

    Uses loads/ingredientspends (not loads/fedmixes). Weights are kilograms.
    Each row includes farm (CM/GAD) from the nested ration breakdown.
    """
    from_utc, to_utc = _utc_month_range(period_start, period_end)
    param_candidates = (
        {"from": from_utc, "to": to_utc},
        {"startDate": from_utc, "endDate": to_utc},
        {"fromDate": from_utc, "toDate": to_utc},
    )
    merged: dict[tuple[str, str], dict[str, float | str]] = {}

    with httpx.Client(timeout=120.0) as client:
        access_token, farm_ids = _authenticate_farms(db, client)
        from app.services.feed_usage_settings import (
            ingredient_inclusion_lookup,
            ration_farm_lookup,
        )

        ration_lookup = ration_farm_lookup(db)
        ingredient_inclusion = ingredient_inclusion_lookup(db)
        forage_type_ids = _forage_ingredient_type_ids(
            client, _api_headers(access_token, farm_ids[0])
        )
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
            for row in _parse_ingredient_spends_by_farm(
                response.json(),
                forage_type_ids=forage_type_ids,
                ration_lookup=ration_lookup,
                ingredient_inclusion=ingredient_inclusion,
            ):
                key = (str(row["farm"]), str(row["ingredient_name"]))
                current = merged.setdefault(
                    key,
                    {
                        "farm": row["farm"],
                        "ingredient_name": row["ingredient_name"],
                        "as_fed_kg": 0.0,
                        "dm_kg": 0.0,
                        "cost": 0.0,
                    },
                )
                current["as_fed_kg"] = float(current["as_fed_kg"]) + float(row["as_fed_kg"])
                current["dm_kg"] = float(current["dm_kg"]) + float(row["dm_kg"])
                current["cost"] = float(current["cost"]) + float(row["cost"])

    rows = list(merged.values())
    rows.sort(
        key=lambda row: (str(row["farm"]), -float(row["as_fed_kg"]), str(row["ingredient_name"]))
    )
    return rows
