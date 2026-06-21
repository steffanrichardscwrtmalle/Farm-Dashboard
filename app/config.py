"""Application configuration from environment variables."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
# Render Secret Files (Environment → Secret Files → filename `.env`)
load_dotenv("/etc/secrets/.env")


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite:///data/wynnstay.db")
)

SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT.lower() == "production"

SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 12)))  # 12 hours
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "12"))

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Microsoft Graph (OneDrive / SharePoint herd export files)
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID", "").strip()
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "").strip()
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "").strip()
GRAPH_DRIVE_USER_EMAIL = os.getenv("GRAPH_DRIVE_USER_EMAIL", "").strip().lower()
HERD_EXPORT_BASE_PATH = os.getenv(
    "HERD_EXPORT_BASE_PATH",
    "Power BI Reports/Cwrt Malle and GAD Costings",
).strip().strip("/")

# Optional local folder for development (skips Graph when set)
LOCAL_HERD_EXPORT_DIR = os.getenv("LOCAL_HERD_EXPORT_DIR", "").strip()

# Secured import endpoint for cron / automation
IMPORT_API_KEY = os.getenv("IMPORT_API_KEY", "").strip()

# Feedlync API (refresh token from browser MSAL storage after logging in once)
FEEDLYNC_REFRESH_TOKEN = os.getenv("FEEDLYNC_REFRESH_TOKEN", "").strip()
FEEDLYNC_FARM_ID = os.getenv("FEEDLYNC_FARM_ID", "").strip()
FEEDLYNC_API_BASE = os.getenv(
    "FEEDLYNC_API_BASE", "https://api-eu.feedlync.com/api/v1"
).strip().rstrip("/")
FEEDLYNC_TOKEN_URL = os.getenv(
    "FEEDLYNC_TOKEN_URL",
    "https://abagrilink.b2clogin.com/abagrilink.onmicrosoft.com/"
    "b2c_1_feedlyncsignupsignin/oauth2/v2.0/token",
).strip()
FEEDLYNC_CLIENT_ID = os.getenv(
    "FEEDLYNC_CLIENT_ID", "6874a800-aff4-4f4d-99d1-1ff47241fe7f"
).strip()
FEEDLYNC_TOKEN_SCOPE = os.getenv(
    "FEEDLYNC_TOKEN_SCOPE",
    "https://ABAgriLink.onmicrosoft.com/dairyapidev/admin "
    "https://ABAgriLink.onmicrosoft.com/dairyapidev/write "
    "openid profile offline_access",
).strip()

COOKIE_SECURE = IS_PRODUCTION or os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
