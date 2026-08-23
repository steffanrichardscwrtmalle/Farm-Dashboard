"""SenseHub (SCR Allflex) HTTP client.

Talks to the same REST API the https://st.scrdairy.com Angular app uses:
login, then reports through the farm reverse-proxy.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import uuid
from typing import Any

import httpx

from app.config import (
    SENSEHUB_DOMAIN_RESOLVER_URL,
    SENSEHUB_FARM_ID,
    SENSEHUB_PASSWORD,
    SENSEHUB_PROXY_BASE,
    SENSEHUB_REGION,
    SENSEHUB_USERNAME,
    sensehub_is_configured,
)

REPORT_TITLES: dict[str, str] = {
    "AnimalsInHeat": "Animals in heat",
    "AnimalsToInspect": "Animals to inspect",
    "AnimalDistress": "Animal distress",
    "Health": "Health",
    "YoungStockHealth": "Young Stock Health by Age",
    "Young Stock Health by Age All": "Young Stock Health by Age All",
    "Young Stock Health by Age": "Young Stock Health by Age",
    "AnestrusCows": "Anestrus cows",
    "IrregularHeats": "Irregular heats",
    "SuspectedForAbortion": "Suspected for abortion",
    "PregnancyChance": "Pregnancy chance",
    "HeatReportHiddenByGroupActivityReport": "Heat hidden by group activity",
    "FertilityOverview": "Fertility overview",
    "ExpectedCalvingAndDryOff": "Expected calving and dry-off",
    "EarlyFreshCows": "Early fresh cows",
    "GroupRoutine": "Group routine",
    "GroupRoutineHeatStress": "Group routine — heat stress",
    "TagMaintenanceCalls": "Tag maintenance",
    "Tags": "Tags",
    "ConnectivityProblems": "Connectivity issues",
}

DEFAULT_REPORT = "Young Stock Health by Age All"

FIELD_LABELS: dict[str, str] = {
    "AnimalID": "Animal ID",
    "YoungStockHealthIndex": "Health index",
    "AgeInDays": "Age (days)",
    "DailyRumination": "Daily rumination",
    "DailyEatingTime": "Daily eating",
    "CowGroupName": "Group",
    "GroupName": "Group",
}

_YOUNG_STOCK_COLUMNS = (
    "AnimalID",
    "YoungStockHealthIndex",
    "AgeInDays",
    "DailyEatingTime",
    "DailyRumination",
    "CowGroupName",
    "GroupName",
)

REPORT_COLUMN_ORDER: dict[str, tuple[str, ...]] = {
    "YoungStockHealth": _YOUNG_STOCK_COLUMNS,
    "Young Stock Health by Age All": _YOUNG_STOCK_COLUMNS,
    "Young Stock Health by Age": _YOUNG_STOCK_COLUMNS,
}

PRIORITY_REPORTS: tuple[str, ...] = (
    "Young Stock Health by Age All",
    "Young Stock Health by Age",
    "YoungStockHealth",
    "AnimalsInHeat",
    "AnimalsToInspect",
    "AnimalDistress",
    "Health",
    "AnestrusCows",
    "EarlyFreshCows",
    "ExpectedCalvingAndDryOff",
)

_HIDDEN_FIELD_SUFFIXES = ("Badge",)
_HIDDEN_FIELDS = {
    "rowNumber",
    "rowId",
    "rowType",
    "parentRowId",
    "rowNumberInGroup",
    "CowDatabaseID",
    "ExternalID",
    "CowDbId",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class SenseHubError(Exception):
    """SenseHub API or configuration error."""


class SenseHubAuthError(SenseHubError):
    """Login failed or credentials are missing."""


class SenseHubConfigError(SenseHubError):
    """Username / password / farm ID not configured."""


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _basic_auth(username: str, farm_id: str, region: str, password: str) -> str:
    raw = f"{username}_{farm_id}_{region}:{password}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _correlation_id() -> str:
    return str(uuid.uuid4())


def _client_headers(
    *,
    token: str | None = None,
    display_version: str | None = None,
) -> dict[str, str]:
    farm_id = SENSEHUB_FARM_ID
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    version = display_version or "dashboard"
    headers = {
        "farmId": farm_id,
        "SCR-Farm-Id": farm_id,
        "scr-client-correlationid": _correlation_id(),
        "Accept": "application/json",
        "SCR-Client-Time": json.dumps({"dateTime": now}),
        "SCR-Client-Type": json.dumps(
            {"name": "optional", "type": "web", "version": version}
        ),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def humanize_field(name: str) -> str:
    stripped = name.removesuffix("Calculation")
    if stripped in FIELD_LABELS:
        return FIELD_LABELS[stripped]
    spaced = _CAMEL_RE.sub(" ", stripped.replace("ID", " ID"))
    return re.sub(r"\s+", " ", spaced).strip()


def field_key(name: str) -> str:
    return name.removesuffix("Calculation")


def _is_unix_seconds(value: Any) -> bool:
    return isinstance(value, (int, float)) and 1_000_000_000 <= float(value) < 4_000_000_000


def _stringify(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        primary = (
            value.get("mostImportantReport")
            or value.get("mostImportantReport")
            or value.get("primaryReport")
            or value.get("name")
            or value.get("value")
        )
        rest = (
            value.get("restOfReports")
            or value.get("restOfReports")
            or value.get("otherReports")
            or []
        )
        if primary:
            extras = [str(item) for item in rest if item]
            return ", ".join([str(primary), *extras]) if extras else str(primary)
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


def flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in row.items():
        key = field_key(str(raw_key))
        if key in _HIDDEN_FIELDS or key.endswith(_HIDDEN_FIELD_SUFFIXES):
            continue
        if _is_unix_seconds(value) and any(
            token in key.lower() for token in ("time", "updated", "date")
        ):
            out[key] = dt.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
        else:
            out[key] = _stringify(value)
    return out


def flatten_report(raw: dict[str, Any], *, catalog_item: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _unwrap(raw) or {}
    meta = body.get("meta") or body.get("meta") or {}
    rows_in = body.get("rows") or body.get("rows") or []
    rows = [flatten_row(row) for row in rows_in if isinstance(row, dict)]
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    name = (
        (catalog_item or {}).get("name")
        or meta.get("reportName")
        or meta.get("reportName")
        or "Report"
    )
    preferred = REPORT_COLUMN_ORDER.get(name, ())
    ordered = [key for key in preferred if key in seen]
    ordered.extend(key for key in seen if key not in ordered)
    columns = [{"key": key, "label": humanize_field(key)} for key in ordered]
    return {
        "report_key": int(
            (catalog_item or {}).get("key")
            or meta.get("reportId")
            or meta.get("reportId")
            or 0
        ),
        "report_name": name,
        "title": REPORT_TITLES.get(name, name if " " in str(name) else humanize_field(name)),
        "category": (catalog_item or {}).get("category")
        or ("Custom" if (catalog_item or {}).get("is_custom") else None),
        "row_count": int(
            meta.get("rowCount")
            or meta.get("rowCount")
            or len(rows)
        ),
        "report_time": meta.get("reportTime")
        or meta.get("reportTime"),
        "columns": columns,
        "rows": rows,
    }


def login(client: httpx.Client) -> tuple[str, str | None]:
    if not sensehub_is_configured():
        raise SenseHubConfigError(
            "SenseHub is not configured. Set SENSEHUB_USERNAME, "
            "SENSEHUB_PASSWORD and SENSEHUB_FARM_ID."
        )
    headers = {
        "Authorization": _basic_auth(
            SENSEHUB_USERNAME, SENSEHUB_FARM_ID, SENSEHUB_REGION, SENSEHUB_PASSWORD
        ),
        "farmId": SENSEHUB_FARM_ID,
        "Content-Type": "application/json",
        "scr-client-correlationid": _correlation_id(),
        "Accept": "application/json",
    }
    response = client.post(
        f"{SENSEHUB_PROXY_BASE}/rest/api/v4/auth/login",
        headers=headers,
        json={"username": SENSEHUB_USERNAME, "password": SENSEHUB_PASSWORD},
    )
    if response.status_code in (401, 403):
        raise SenseHubAuthError("SenseHub rejected the username, password, or farm ID.")
    if response.status_code >= 400:
        raise SenseHubError(f"SenseHub login failed ({response.status_code}).")
    body = _unwrap(response.json()) or {}
    token = body.get("accessToken")
    if not token:
        raise SenseHubAuthError("SenseHub login did not return an access token.")
    display_version = body.get("displayVersion") or body.get("displayVersion")
    return str(token), str(display_version) if display_version else None


def catalog_from_v5_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """System reports plus Custom reports (Menu → Reports → Custom reports)."""
    items: list[dict[str, Any]] = []
    for item in body.get("reports") or []:
        if isinstance(item, dict):
            tagged = dict(item)
            tagged["is_custom"] = False
            items.append(tagged)
    for item in body.get("customReports") or []:
        if isinstance(item, dict):
            tagged = dict(item)
            tagged["is_custom"] = True
            tagged.setdefault("category", "Custom")
            items.append(tagged)
    return items


def list_reports(client: httpx.Client, token: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/v5/reports",
        headers=_client_headers(token=token),
    )
    response.raise_for_status()
    body = _unwrap(response.json()) or {}
    if not isinstance(body, dict):
        return []
    return catalog_from_v5_body(body)


def fetch_report(
    client: httpx.Client,
    token: str,
    report_key: int,
    *,
    cloud: bool = False,
    display_version: str | None = None,
) -> dict[str, Any]:
    version_path = "v3" if cloud else "v2"
    headers = _client_headers(
        token=token,
        display_version=(display_version or "8.3.2.357") if cloud else None,
    )
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/{version_path}/reports/{report_key}",
        headers=headers,
        params={"offset": 0, "limit": 0, "type": "full"},
    )
    response.raise_for_status()
    return response.json()


def fetch_farm_about(client: httpx.Client, token: str) -> dict[str, Any]:
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/system/about",
        headers=_client_headers(token=token),
    )
    if response.status_code >= 400:
        return {}
    body = _unwrap(response.json()) or {}
    return body if isinstance(body, dict) else {}


def fetch_named_reports(
    names: list[str],
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Log in and download only the named SenseHub reports."""
    return fetch_all_reports(client=client, names=names)


def fetch_all_reports(
    client: httpx.Client | None = None,
    *,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Log in and download SenseHub reports. Returns flattened snapshots."""
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        about = fetch_farm_about(client, token)
        catalog = list_reports(client, token)
        if names:
            wanted = {name.casefold() for name in names}
            catalog = [
                item
                for item in catalog
                if str(item.get("name") or "").casefold() in wanted
            ]
            if not catalog:
                raise SenseHubError(
                    "SenseHub catalogue does not include: " + ", ".join(names)
                )
        reports: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in catalog:
            key = item.get("key")
            if key is None:
                continue
            try:
                raw = fetch_report(
                    client,
                    token,
                    int(key),
                    cloud=bool(item.get("is_custom")),
                    display_version=display_version,
                )
                reports.append(flatten_report(raw, catalog_item=item))
            except httpx.HTTPError as exc:
                errors.append(f"{item.get('name') or key}: {exc}")
        required = names[0] if names else DEFAULT_REPORT
        if not any(
            str(item.get("report_name") or "").casefold() == required.casefold()
            for item in reports
        ):
            detail = "; ".join(errors[:3]) if errors else "report missing from catalog"
            raise SenseHubError(
                f"Could not fetch {required} ({detail})."
            )
        return {
            "farm_id": about.get("farmId") or SENSEHUB_FARM_ID,
            "farm_name": about.get("farmName"),
            "software_version": about.get("version"),
            "reports": reports,
        }
    finally:
        if own_client:
            client.close()


def resolve_proxy_domain(farm_id: str | None = None) -> str:
    """Public helper used by tests; login already uses the resolved default proxy."""
    farm = farm_id or SENSEHUB_FARM_ID
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(
            SENSEHUB_DOMAIN_RESOLVER_URL,
            headers={"farmId": farm, "Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        return str(body.get("domain") or SENSEHUB_PROXY_BASE)
