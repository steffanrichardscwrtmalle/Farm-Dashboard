"""Sync and map Xero organisations (tenants) to dashboard businesses."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BUSINESS_OPTIONS, XeroOrganisation
from app.services.xero_auth import (
    XeroAuthError,
    get_stored_access_token,
    get_stored_refresh_token,
    save_tokens,
)
from app.services.xero_oauth import fetch_connections, refresh_access_token


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def list_organisations(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(XeroOrganisation).order_by(XeroOrganisation.tenant_name.asc())
    ).all()
    return [
        {
            "tenant_id": row.tenant_id,
            "tenant_name": row.tenant_name,
            "tenant_type": row.tenant_type,
            "dashboard_business": row.dashboard_business,
            "is_active": row.is_active,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


def upsert_connections(db: Session, connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = _utcnow()
    seen: set[str] = set()
    for item in connections:
        tenant_id = str(item.get("tenantId") or "").strip()
        if not tenant_id:
            continue
        seen.add(tenant_id)
        row = db.scalar(
            select(XeroOrganisation).where(XeroOrganisation.tenant_id == tenant_id)
        )
        name = str(item.get("tenantName") or tenant_id).strip()
        tenant_type = str(item.get("tenantType") or "").strip() or None
        if row is None:
            db.add(
                XeroOrganisation(
                    tenant_id=tenant_id,
                    tenant_name=name,
                    tenant_type=tenant_type,
                    is_active=True,
                    connected_at=now,
                    updated_at=now,
                )
            )
        else:
            row.tenant_name = name
            row.tenant_type = tenant_type
            row.is_active = True
            row.updated_at = now

    existing = db.scalars(select(XeroOrganisation)).all()
    for row in existing:
        if row.tenant_id not in seen:
            row.is_active = False
            row.updated_at = now
    db.commit()
    return list_organisations(db)


def set_dashboard_business(
    db: Session,
    *,
    tenant_id: str,
    dashboard_business: str | None,
) -> dict[str, Any]:
    row = db.scalar(
        select(XeroOrganisation).where(XeroOrganisation.tenant_id == tenant_id)
    )
    if row is None:
        raise XeroAuthError("Unknown Xero organisation.")
    value = (dashboard_business or "").strip() or None
    if value and value not in BUSINESS_OPTIONS:
        raise XeroAuthError(
            f"Dashboard business must be one of: {', '.join(BUSINESS_OPTIONS)}"
        )
    # One tenant per dashboard business.
    if value:
        others = db.scalars(
            select(XeroOrganisation).where(
                XeroOrganisation.dashboard_business == value,
                XeroOrganisation.tenant_id != tenant_id,
            )
        ).all()
        for other in others:
            other.dashboard_business = None
            other.updated_at = _utcnow()
    row.dashboard_business = value
    row.updated_at = _utcnow()
    db.commit()
    return {
        "tenant_id": row.tenant_id,
        "tenant_name": row.tenant_name,
        "tenant_type": row.tenant_type,
        "dashboard_business": row.dashboard_business,
        "is_active": row.is_active,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def clear_organisations(db: Session) -> None:
    rows = db.scalars(select(XeroOrganisation)).all()
    for row in rows:
        db.delete(row)
    db.commit()


def resolve_access_token(db: Session) -> str:
    access_token, expires_at = get_stored_access_token(db)
    now = _utcnow()
    if access_token and expires_at and expires_at > now:
        return access_token

    refresh_token = get_stored_refresh_token(db)
    if not refresh_token:
        raise XeroAuthError("Xero is not connected. Connect Xero from Office Admin → Xero.")

    with httpx.Client(timeout=60.0) as client:
        payload = refresh_access_token(client, refresh_token=refresh_token)
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    save_tokens(
        db,
        refresh_token=new_refresh,
        access_token=str(payload.get("access_token") or ""),
        expires_in=int(payload.get("expires_in") or 1800),
    )
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise XeroAuthError("Xero refresh did not return an access token.")
    return access


def sync_organisations_from_xero(db: Session) -> list[dict[str, Any]]:
    access_token = resolve_access_token(db)
    with httpx.Client(timeout=60.0) as client:
        connections = fetch_connections(client, access_token=access_token)
    return upsert_connections(db, connections)
