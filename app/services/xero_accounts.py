"""Sync Xero chart of accounts for category labels on actuals."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import XERO_API_BASE_URL
from app.models import XeroAccount, XeroOrganisation
from app.services.xero_auth import XeroAuthError
from app.services.xero_orgs import resolve_access_token


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def fetch_accounts(
    client: httpx.Client,
    *,
    access_token: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{XERO_API_BASE_URL}/Accounts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        },
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise XeroAuthError(
            f"Failed to fetch accounts for tenant {tenant_id[:8]}… ({detail[:240]})"
        )
    payload = response.json() or {}
    return list(payload.get("Accounts") or [])


def upsert_accounts(
    db: Session,
    *,
    tenant_id: str,
    accounts: list[dict[str, Any]],
) -> int:
    now = _utcnow()
    count = 0
    for item in accounts:
        account_id = str(item.get("AccountID") or "").strip()
        if not account_id:
            continue
        row = db.scalar(
            select(XeroAccount)
            .where(XeroAccount.tenant_id == tenant_id)
            .where(XeroAccount.account_id == account_id)
        )
        values = {
            "code": (str(item.get("Code") or "").strip() or None),
            "name": (str(item.get("Name") or "").strip() or account_id),
            "account_type": (str(item.get("Type") or "").strip() or None),
            "account_class": (str(item.get("Class") or "").strip() or None),
            "status": (str(item.get("Status") or "").strip() or None),
            "synced_at": now,
        }
        if row is None:
            db.add(XeroAccount(tenant_id=tenant_id, account_id=account_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        count += 1
    db.commit()
    return count


def sync_accounts_for_organisation(
    db: Session,
    client: httpx.Client,
    *,
    access_token: str,
    organisation: XeroOrganisation,
) -> dict[str, Any]:
    accounts = fetch_accounts(
        client,
        access_token=access_token,
        tenant_id=organisation.tenant_id,
    )
    count = upsert_accounts(db, tenant_id=organisation.tenant_id, accounts=accounts)
    return {
        "tenant_id": organisation.tenant_id,
        "tenant_name": organisation.tenant_name,
        "fetched": count,
    }


def sync_all_accounts(db: Session) -> dict[str, Any]:
    organisations = list(
        db.scalars(
            select(XeroOrganisation)
            .where(XeroOrganisation.is_active.is_(True))
            .where(XeroOrganisation.dashboard_business.isnot(None))
            .order_by(XeroOrganisation.tenant_name.asc())
        ).all()
    )
    if not organisations:
        raise XeroAuthError(
            "Map at least one Xero organisation to a dashboard business before syncing."
        )
    access_token = resolve_access_token(db)
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=60.0) as client:
        for organisation in organisations:
            results.append(
                sync_accounts_for_organisation(
                    db,
                    client,
                    access_token=access_token,
                    organisation=organisation,
                )
            )
    return {
        "organisations": results,
        "fetched_total": sum(int(item["fetched"]) for item in results),
    }


def account_name_lookup(db: Session, *, tenant_ids: list[str] | None = None) -> dict[str, str]:
    """Map account_code → name (first match wins if codes collide across tenants)."""
    return {
        code: meta["name"]
        for code, meta in account_meta_lookup(db, tenant_ids=tenant_ids).items()
    }


def account_meta_lookup(
    db: Session, *, tenant_ids: list[str] | None = None
) -> dict[str, dict[str, str | None]]:
    """Map account_code → {name, account_class, account_type}."""
    stmt = select(
        XeroAccount.code,
        XeroAccount.name,
        XeroAccount.account_class,
        XeroAccount.account_type,
    ).where(XeroAccount.code.isnot(None))
    if tenant_ids:
        stmt = stmt.where(XeroAccount.tenant_id.in_(tenant_ids))
    lookup: dict[str, dict[str, str | None]] = {}
    for code, name, account_class, account_type in db.execute(stmt).all():
        key = str(code).strip()
        if not key or key in lookup:
            continue
        lookup[key] = {
            "name": (str(name or key).strip() or key),
            "account_class": (
                str(account_class).strip().upper() if account_class else None
            ),
            "account_type": (str(account_type).strip().upper() if account_type else None),
        }
    return lookup


def clear_accounts(db: Session) -> None:
    db.execute(delete(XeroAccount))
    db.commit()
