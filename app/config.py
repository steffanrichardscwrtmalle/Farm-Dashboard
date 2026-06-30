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

# NML milk-quality results (emailed PDF reports from National Milk Laboratories).
# Each farm's results arrive in a separate mailbox; requires Graph Mail.Read.
NML_SENDER = os.getenv("NML_SENDER", "milk.autoemail@nationalmilklabs.com").strip().lower()
NML_MAILBOX_GAD = os.getenv("NML_MAILBOX_GAD", "steff@greenacredairy.co.uk").strip().lower()
NML_MAILBOX_CM = os.getenv("NML_MAILBOX_CM", "steff@cwrtmalle.co.uk").strip().lower()
# How many days of mail to scan on each import run.
NML_LOOKBACK_DAYS = int(os.getenv("NML_LOOKBACK_DAYS", "30"))
# Optional local folder of PDFs for development (skips Graph mail when set).
LOCAL_NML_DIR = os.getenv("LOCAL_NML_DIR", "").strip()

# Cwrt Malle mailbox lives in a separate Microsoft 365 tenant, so it needs its
# own app registration (Mail.Read). When these are set, the CM mailbox is read
# with these credentials; the GAD mailbox keeps using the GRAPH_* app above.
GRAPH_TENANT_ID_CM = os.getenv("GRAPH_TENANT_ID_CM", "").strip()
GRAPH_CLIENT_ID_CM = os.getenv("GRAPH_CLIENT_ID_CM", "").strip()
GRAPH_CLIENT_SECRET_CM = os.getenv("GRAPH_CLIENT_SECRET_CM", "").strip()


def graph_cm_is_configured() -> bool:
    return bool(GRAPH_TENANT_ID_CM and GRAPH_CLIENT_ID_CM and GRAPH_CLIENT_SECRET_CM)


# Milk haulier collection reports (emailed XLSX from Richard Thomas Transport).
# Arrives near-daily as a running monthly spreadsheet; we upsert on each import.
# The Cwrt Malle report lands in the CM mailbox (separate tenant), so it reuses
# the GRAPH_*_CM credentials when configured.
HAULIER_SENDER = os.getenv(
    "HAULIER_SENDER", "rhodri@richardthomastransport.co.uk"
).strip().lower()
# The haulier emails from several people at the same domain, so match the domain
# rather than a single address. Leading '@' is optional.
HAULIER_SENDER_DOMAIN = os.getenv(
    "HAULIER_SENDER_DOMAIN", "richardthomastransport.co.uk"
).strip().lower().lstrip("@")
HAULIER_MAILBOX_CM = os.getenv("HAULIER_MAILBOX_CM", "steff@cwrtmalle.co.uk").strip().lower()
HAULIER_MAILBOX_GAD = os.getenv("HAULIER_MAILBOX_GAD", "").strip().lower()
# How many days of mail to scan on each import run.
HAULIER_LOOKBACK_DAYS = int(os.getenv("HAULIER_LOOKBACK_DAYS", "30"))
# Optional local folder of XLSX reports for development (skips Graph mail when set).
LOCAL_HAULIER_DIR = os.getenv("LOCAL_HAULIER_DIR", "").strip()

# Milk buyer monthly sales statements (emailed PDFs).
# Freshways statements arrive at the GAD mailbox; Dairy Partners at the CM mailbox.
STATEMENTS_FRESHWAYS_DOMAIN = os.getenv(
    "STATEMENTS_FRESHWAYS_DOMAIN", "freshways.co.uk"
).strip().lower().lstrip("@")
STATEMENTS_DAIRYPARTNERS_DOMAIN = os.getenv(
    "STATEMENTS_DAIRYPARTNERS_DOMAIN", "dairypartners.co.uk"
).strip().lower().lstrip("@")
STATEMENTS_MAILBOX_GAD = os.getenv(
    "STATEMENTS_MAILBOX_GAD", NML_MAILBOX_GAD
).strip().lower()
STATEMENTS_MAILBOX_CM = os.getenv(
    "STATEMENTS_MAILBOX_CM", NML_MAILBOX_CM
).strip().lower()
STATEMENTS_LOOKBACK_DAYS = int(os.getenv("STATEMENTS_LOOKBACK_DAYS", "30"))
STATEMENTS_DEFAULT_HAULAGE = float(os.getenv("STATEMENTS_DEFAULT_HAULAGE", "1.0"))
# Optional local folder of statement PDFs for development (skips Graph mail when set).
LOCAL_STATEMENTS_DIR = os.getenv("LOCAL_STATEMENTS_DIR", "").strip()

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
PUBLIC_APP_URL = os.getenv(
    "PUBLIC_APP_URL", os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
).strip().rstrip("/")
FEEDLYNC_AUTHORIZE_URL = os.getenv(
    "FEEDLYNC_AUTHORIZE_URL",
    "https://abagrilink.b2clogin.com/abagrilink.onmicrosoft.com/"
    "b2c_1_feedlyncsignupsignin/oauth2/v2.0/authorize",
).strip()
FEEDLYNC_REDIRECT_URI = os.getenv(
    "FEEDLYNC_REDIRECT_URI",
    f"{PUBLIC_APP_URL}/api/feedlync/oauth/callback",
).strip()

# Unattended login (B2C self-asserted flow replication). The redirect URI here
# must be one already registered by FeedLync for their SPA client; we intercept
# the redirect server-side and never actually load the page.
FEEDLYNC_USERNAME = os.getenv("FEEDLYNC_USERNAME", "").strip()
FEEDLYNC_PASSWORD = os.getenv("FEEDLYNC_PASSWORD", "")
FEEDLYNC_SPA_REDIRECT_URI = os.getenv(
    "FEEDLYNC_SPA_REDIRECT_URI",
    "https://app.feedlync.com/redirect.html",
).strip()

# DocuSeal (HR contract signing)
DOCUSEAL_API_KEY = os.getenv("DOCUSEAL_API_KEY", "").strip()
DOCUSEAL_BASE_URL = os.getenv(
    "DOCUSEAL_BASE_URL", "https://api.docuseal.com"
).strip().rstrip("/")
DOCUSEAL_WEBHOOK_SECRET = os.getenv("DOCUSEAL_WEBHOOK_SECRET", "").strip()
HR_ENCRYPTION_KEY = os.getenv("HR_ENCRYPTION_KEY", "").strip()
# Global HR reviewer emails (fallback) + per-business overrides.
HR_HR_TEAM_EMAILS = os.getenv("HR_HR_TEAM_EMAILS", "").strip()
HR_HR_TEAM_EMAILS_CWRTMALLE = os.getenv("DOCUSEAL_CM_HR_TEAM_EMAILS", "").strip()
HR_HR_TEAM_EMAILS_GREENACRE = os.getenv("DOCUSEAL_GAD_HR_TEAM_EMAILS", "").strip()
CONTRACTS_STORAGE_DIR = os.getenv(
    "CONTRACTS_STORAGE_DIR",
    "/var/data/contracts" if IS_PRODUCTION else str(_PROJECT_ROOT / "data" / "contracts"),
).strip()
DOCUSEAL_CWRTMALLE_TEMPLATE_ID = os.getenv("DOCUSEAL_CWRTMALLE_TEMPLATE_ID", "").strip()
DOCUSEAL_CWRTMALLE_TEMPLATE_NAME = os.getenv(
    "DOCUSEAL_CWRTMALLE_TEMPLATE_NAME", "Cwrt Malle Employment Contract"
).strip()
DOCUSEAL_GREENACRE_TEMPLATE_ID = os.getenv("DOCUSEAL_GREENACRE_TEMPLATE_ID", "").strip()
DOCUSEAL_GREENACRE_TEMPLATE_NAME = os.getenv(
    "DOCUSEAL_GREENACRE_TEMPLATE_NAME", "Green Acre Dairy Employment Contract"
).strip()
# Customise the email DocuSeal sends to signers. Leave blank to use DocuSeal's
# defaults. Body supports DocuSeal tags e.g. {{template.name}}, {{submitter.link}}.
# Global values are the fallback; per-business values override them.


def _email_env(name: str) -> str:
    """Read an email body/subject env var, decoding literal \\n escapes.

    Render passes env values raw, so a literal backslash-n stays as two
    characters; local .env (python-dotenv, double-quoted) already decodes them.
    Normalise both so newlines render correctly in the signer email.
    """
    raw = os.getenv(name, "").strip()
    return raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


DOCUSEAL_EMAIL_SUBJECT = _email_env("DOCUSEAL_EMAIL_SUBJECT")
DOCUSEAL_EMAIL_BODY = _email_env("DOCUSEAL_EMAIL_BODY")
DOCUSEAL_CWRTMALLE_EMAIL_SUBJECT = _email_env("DOCUSEAL_CWRTMALLE_EMAIL_SUBJECT")
DOCUSEAL_CWRTMALLE_EMAIL_BODY = _email_env("DOCUSEAL_CWRTMALLE_EMAIL_BODY")
DOCUSEAL_GREENACRE_EMAIL_SUBJECT = _email_env("DOCUSEAL_GREENACRE_EMAIL_SUBJECT")
DOCUSEAL_GREENACRE_EMAIL_BODY = _email_env("DOCUSEAL_GREENACRE_EMAIL_BODY")


def _hr_business_key(business: str | None) -> str | None:
    """Map a full business name to its config suffix (CWRTMALLE / GREENACRE)."""
    b = (business or "").strip().lower()
    if b.startswith("cwrt malle"):
        return "CWRTMALLE"
    if b.startswith("green acre"):
        return "GREENACRE"
    return None


def hr_team_emails_for(business: str | None) -> str:
    """HR reviewer emails for a business, falling back to the global list."""
    key = _hr_business_key(business)
    if key == "CWRTMALLE" and HR_HR_TEAM_EMAILS_CWRTMALLE:
        return HR_HR_TEAM_EMAILS_CWRTMALLE
    if key == "GREENACRE" and HR_HR_TEAM_EMAILS_GREENACRE:
        return HR_HR_TEAM_EMAILS_GREENACRE
    return HR_HR_TEAM_EMAILS


def docuseal_email_for(business: str | None) -> tuple[str, str]:
    """(subject, body) for a business' signer email, falling back to global."""
    key = _hr_business_key(business)
    if key == "CWRTMALLE":
        return (
            DOCUSEAL_CWRTMALLE_EMAIL_SUBJECT or DOCUSEAL_EMAIL_SUBJECT,
            DOCUSEAL_CWRTMALLE_EMAIL_BODY or DOCUSEAL_EMAIL_BODY,
        )
    if key == "GREENACRE":
        return (
            DOCUSEAL_GREENACRE_EMAIL_SUBJECT or DOCUSEAL_EMAIL_SUBJECT,
            DOCUSEAL_GREENACRE_EMAIL_BODY or DOCUSEAL_EMAIL_BODY,
        )
    return (DOCUSEAL_EMAIL_SUBJECT, DOCUSEAL_EMAIL_BODY)

COOKIE_SECURE = IS_PRODUCTION or os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
