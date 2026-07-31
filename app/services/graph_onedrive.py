"""Download herd export files from OneDrive via Microsoft Graph."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import (
    GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET,
    GRAPH_DRIVE_USER_EMAIL,
    GRAPH_TENANT_ID,
    HERD_EXPORT_BASE_PATH,
    LOCAL_HERD_EXPORT_DIR,
)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Access tokens cached per client_id so multiple app registrations (e.g. a
# second tenant for the Cwrt Malle mailbox) can be used side by side.
_token_cache: dict[str, dict[str, float | str]] = {}


class GraphConfigError(ValueError):
    """Graph API is not configured."""


def graph_is_configured() -> bool:
    return bool(LOCAL_HERD_EXPORT_DIR or (
        GRAPH_TENANT_ID and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET and GRAPH_DRIVE_USER_EMAIL
    ))


def _require_graph_config() -> None:
    missing = [
        name
        for name, val in (
            ("GRAPH_TENANT_ID", GRAPH_TENANT_ID),
            ("GRAPH_CLIENT_ID", GRAPH_CLIENT_ID),
            ("GRAPH_CLIENT_SECRET", GRAPH_CLIENT_SECRET),
            ("GRAPH_DRIVE_USER_EMAIL", GRAPH_DRIVE_USER_EMAIL),
        )
        if not val
    ]
    if missing:
        raise GraphConfigError(
            f"Missing Graph configuration: {', '.join(missing)}. "
            "Set LOCAL_HERD_EXPORT_DIR for local development instead."
        )


def get_access_token_for(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Client-credentials access token for a specific app registration.

    Tokens are cached per client_id so several tenants can be used at once.
    """
    if not (tenant_id and client_id and client_secret):
        raise GraphConfigError(
            "Missing Graph credentials (tenant_id, client_id, client_secret)."
        )

    now = time.time()
    cached = _token_cache.get(client_id)
    if cached and now < float(cached.get("expires_at", 0)) - 60:
        return str(cached.get("access_token", ""))

    url = _TOKEN_URL.format(tenant=tenant_id)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": _GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, data=data)
        response.raise_for_status()
        payload = response.json()

    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    _token_cache[client_id] = {"access_token": token, "expires_at": now + expires_in}
    return token


def get_access_token() -> str:
    """Access token for the default (Green Acre) app registration."""
    _require_graph_config()
    return get_access_token_for(GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET)


def herd_file_relative_path(*parts: str) -> str:
    """Build a path relative to HERD_EXPORT_BASE_PATH, e.g. DCEXPORTCM/CMEVENTS.CSV."""
    segments = [HERD_EXPORT_BASE_PATH, *parts] if HERD_EXPORT_BASE_PATH else list(parts)
    return "/".join(seg.strip("/") for seg in segments if seg)


def find_newest_herd_file_meta(
    folder_relative_path: str, *, suffix: str | None = None
) -> dict[str, str]:
    """
    Return metadata for the most recently modified file in a herd folder.

    Keys: ``relative_path`` (for download_herd_file), ``name``, ``last_modified``
    (ISO-8601 string from Graph, or UTC ISO from local mtime).
    """
    from datetime import datetime, timezone

    suffix_lower = suffix.lower() if suffix else None

    if LOCAL_HERD_EXPORT_DIR:
        folder = Path(LOCAL_HERD_EXPORT_DIR).joinpath(*folder_relative_path.split("/"))
        if not folder.is_dir():
            raise FileNotFoundError(f"Local herd folder not found: {folder}")
        candidates = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and not path.name.startswith("~$")
            and (suffix_lower is None or path.suffix.lower() == suffix_lower)
        ]
        if not candidates:
            raise FileNotFoundError(
                f"No matching files in local herd folder: {folder}"
            )
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
        return {
            "relative_path": f"{folder_relative_path}/{newest.name}",
            "name": newest.name,
            "last_modified": mtime.isoformat().replace("+00:00", "Z"),
        }

    _require_graph_config()
    full_path = herd_file_relative_path(folder_relative_path)
    encoded_path = quote(full_path, safe="/")
    url = (
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_DRIVE_USER_EMAIL}"
        f"/drive/root:/{encoded_path}:/children"
        "?$select=name,file,lastModifiedDateTime&$top=200"
    )

    token = get_access_token()
    items: list[dict] = []
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        while url:
            response = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 404:
                raise FileNotFoundError(f"OneDrive folder not found: {full_path}")
            response.raise_for_status()
            payload = response.json()
            items.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")

    files = [
        item
        for item in items
        if item.get("file")
        and not item.get("name", "").startswith("~$")
        and (suffix_lower is None or item.get("name", "").lower().endswith(suffix_lower))
    ]
    if not files:
        raise FileNotFoundError(
            f"No matching files in OneDrive folder: {full_path}"
        )
    newest = max(files, key=lambda item: item.get("lastModifiedDateTime", ""))
    return {
        "relative_path": f"{folder_relative_path}/{newest['name']}",
        "name": newest["name"],
        "last_modified": str(newest.get("lastModifiedDateTime") or ""),
    }


def find_newest_herd_file(folder_relative_path: str, *, suffix: str | None = None) -> str:
    """
    Return the relative path of the most recently modified file in a herd folder.

    folder_relative_path is under HERD_EXPORT_BASE_PATH, e.g. 'Genomic Results'.
    When suffix is given (e.g. '.xlsx'), only files with that extension are considered.
    The returned value can be passed straight to download_herd_file.
    """
    return find_newest_herd_file_meta(folder_relative_path, suffix=suffix)[
        "relative_path"
    ]


def download_herd_file(relative_path: str) -> bytes:
    """
    Load a herd export file from OneDrive (Graph) or a local synced folder.

    relative_path is under HERD_EXPORT_BASE_PATH, e.g. 'DCEXPORTCM/CMEVENTS.CSV'.
    """
    if LOCAL_HERD_EXPORT_DIR:
        local_path = Path(LOCAL_HERD_EXPORT_DIR).joinpath(*relative_path.split("/"))
        if not local_path.is_file():
            raise FileNotFoundError(f"Local herd file not found: {local_path}")
        return local_path.read_bytes()

    _require_graph_config()
    full_path = herd_file_relative_path(relative_path)
    encoded_path = quote(full_path, safe="/")
    url = (
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_DRIVE_USER_EMAIL}"
        f"/drive/root:/{encoded_path}:/content"
    )

    token = get_access_token()
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        response = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 404:
            raise FileNotFoundError(f"OneDrive file not found: {full_path}")
        response.raise_for_status()
        return response.content
