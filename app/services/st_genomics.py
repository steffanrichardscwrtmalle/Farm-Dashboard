"""Download the ST Strategy GBR female genomic workbook.

Login is a Blazor form, so the session cookie is collected in a headless
browser. The workbook itself comes from ``POST /DairyGBR/ExportFemalesAll``,
which returns the same ``Herd GBR Females`` sheet the OneDrive importer reads.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import ST_STRATEGY_EMAIL, ST_STRATEGY_PASSWORD, st_strategy_is_configured
from app.services.genomic_import import GENOMIC_FOLDER
from app.services.graph_onedrive import upload_herd_file

ST_BASE = "https://strategy.stgen.com"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_EXPORT_BODY_PATH = Path(__file__).with_name("st_gbr_females_export.json")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class StGenomicsError(Exception):
    """ST Strategy request failed."""


class StGenomicsConfigError(StGenomicsError):
    """ST Strategy credentials are not configured."""


class StGenomicsAuthError(StGenomicsError):
    """ST Strategy rejected the login."""


def export_body() -> dict[str, Any]:
    """Column selection captured from the Animal Data GBR 'export all' action."""
    return json.loads(_EXPORT_BODY_PATH.read_text(encoding="utf-8"))


def safe_workbook_name(name: str) -> str:
    """Keep the ST file name and drop any path or OneDrive-illegal characters."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", Path(name).name).strip().strip(".")
    if not cleaned.lower().endswith(".xlsx"):
        raise StGenomicsError(f"ST export file name is not an Excel workbook: {name!r}")
    return cleaned


def workbook_from_export_payload(payload: dict[str, Any]) -> tuple[str, bytes]:
    """Decode the JSON envelope around the base64 xlsx."""
    if not payload.get("success"):
        raise StGenomicsError("ST female export did not succeed.")
    result = payload.get("resultData") or {}
    encoded = result.get("content") or ""
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise StGenomicsError("ST female export did not include a workbook.") from exc
    if not content.startswith(b"PK"):
        raise StGenomicsError("ST female export was not an Excel workbook.")
    return safe_workbook_name(str(result.get("fileName") or "")), content


def store_gbr_females_workbook() -> dict[str, Any]:
    """Log in, download the GBR female workbook, and save it under Genomic Results."""
    file_name, content = download_gbr_females_workbook()
    relative_path = f"{GENOMIC_FOLDER}/{file_name}"
    upload_herd_file(relative_path, content, content_type=XLSX_CONTENT_TYPE)
    return {
        "relative_path": relative_path,
        "file_name": file_name,
        "bytes": len(content),
    }


def download_gbr_females_workbook() -> tuple[str, bytes]:
    """Return ``(file_name, xlsx_bytes)`` for the signed-in herd."""
    if not st_strategy_is_configured():
        raise StGenomicsConfigError(
            "ST Strategy is not configured. Set ST_STRATEGY_EMAIL and ST_STRATEGY_PASSWORD."
        )
    cookies = _login_cookies()
    with httpx.Client(timeout=180.0, follow_redirects=False) as client:
        response = client.post(
            f"{ST_BASE}/DairyGBR/ExportFemalesAll",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": ST_BASE,
                "Referer": f"{ST_BASE}/animal-data-gbr",
                "User-Agent": _USER_AGENT,
                "Cookie": cookies,
            },
            json=export_body(),
        )
    if response.status_code in (401, 403):
        raise StGenomicsAuthError("ST Strategy rejected the session after login.")
    if response.status_code >= 400:
        raise StGenomicsError(f"ST female export failed ({response.status_code}).")
    return workbook_from_export_payload(response.json())


def _login_cookies() -> str:
    """Sign in through the browser form and return a Cookie header value."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise StGenomicsError(
            "Playwright is not installed. The ST fetch cron image installs it."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=_USER_AGENT)
            page = context.new_page()
            page.goto(f"{ST_BASE}/", wait_until="domcontentloaded", timeout=60_000)
            page.locator("#Email").wait_for(timeout=60_000)
            accept = page.get_by_role("button", name="Accept")
            if accept.count() and accept.first.is_visible():
                accept.first.click()
            page.locator("#Email").fill(ST_STRATEGY_EMAIL)
            page.locator("#Password").fill(ST_STRATEGY_PASSWORD)
            page.locator("button[type=submit]").click()
            try:
                page.wait_for_url("**/herd-list**", timeout=60_000)
            except PlaywrightTimeoutError as exc:
                raise StGenomicsAuthError(
                    "ST Strategy did not accept the email and password."
                ) from exc
            pairs = [
                f"{cookie['name']}={cookie['value']}"
                for cookie in context.cookies()
                if cookie.get("name") and cookie.get("value")
            ]
        finally:
            browser.close()

    if not any(pair.startswith("StrategyPortal=") for pair in pairs):
        raise StGenomicsAuthError("ST Strategy login did not return a session cookie.")
    return "; ".join(pairs)
