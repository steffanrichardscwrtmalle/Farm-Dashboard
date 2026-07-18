"""Xero OAuth connect, organisation listing, mapping, and invoice sync."""

from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_page
from app.auth.permissions import PAGE_XERO, has_page
from app.config import XERO_REDIRECT_URI
from app.db import get_db
from app.models import BUSINESS_OPTIONS, User
from app.services.xero_auth import (
    XeroAuthError,
    clear_tokens,
    credentials_configured,
    get_connection_status,
    save_tokens,
)
from app.services.xero_accounts import clear_accounts, sync_all_accounts
from app.services.xero_actuals import available_actual_fiscal_years, list_actuals
from app.services.xero_budget_mappings import (
    clear_account_budget_mappings,
    list_account_budget_mappings,
    mapping_summary,
    set_account_budget_mapping,
)
from app.services.xero_pnl import list_xero_pnl
from app.services.xero_invoices import clear_invoices, invoice_summary, sync_all_invoices
from app.services.xero_journals import clear_journals
from app.services.xero_oauth import (
    build_authorize_url,
    build_oauth_state,
    exchange_authorization_code,
    parse_oauth_state,
)
from app.services.xero_orgs import (
    clear_organisations,
    list_organisations,
    set_dashboard_business,
    sync_organisations_from_xero,
)

router = APIRouter(prefix="/api/xero")


class OrganisationMapBody(BaseModel):
    dashboard_business: str | None = Field(default=None)


class AccountBudgetMapBody(BaseModel):
    mapping_id: int | None = Field(default=None)


@router.get("/status")
def api_xero_status(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    status = get_connection_status(db)
    organisations = list_organisations(db)
    mapped = sum(1 for org in organisations if org.get("dashboard_business"))
    invoices = invoice_summary(db)
    budget_maps = mapping_summary(db)
    stage3_ready = status["connected"] and mapped > 0
    stage3_done = stage3_ready and invoices["invoice_count"] > 0
    stage4_ready = stage3_done and budget_maps["accounts"] > 0
    stage4_done = stage4_ready and budget_maps["unmapped_pnl"] == 0 and budget_maps["pnl_accounts"] > 0
    return {
        **status,
        "redirect_uri": XERO_REDIRECT_URI,
        "business_options": list(BUSINESS_OPTIONS),
        "organisations": organisations,
        "organisation_count": len(organisations),
        "mapped_count": mapped,
        "invoices": invoices,
        "budget_mappings": budget_maps,
        "stages": {
            "1_connect": {
                "ready": status["credentials_configured"],
                "done": status["connected"],
                "label": "Connect Xero",
            },
            "2_organisations": {
                "ready": status["connected"],
                "done": status["connected"] and len(organisations) > 0 and mapped > 0,
                "label": "Link organisations",
            },
            "3_sync": {
                "ready": stage3_ready,
                "done": stage3_done,
                "label": "Sync invoices",
            },
            "4_budget_map": {
                "ready": stage4_ready,
                "done": stage4_done,
                "label": "Map budget headings",
            },
        },
    }


@router.get("/oauth/start")
def api_xero_oauth_start(
    request: Request,
    return_to: str = "/xero",
    user: User = Depends(get_current_user),
):
    if not has_page(user, PAGE_XERO):
        raise HTTPException(status_code=403, detail="You do not have access to this page")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/xero"
    if not credentials_configured():
        return RedirectResponse(
            f"/xero?error={quote('Xero Client ID/Secret are not configured on the server.')}",
            status_code=302,
        )
    try:
        state = build_oauth_state(user_id=user.id, return_to=return_to)
        url = build_authorize_url(state=state)
    except XeroAuthError as exc:
        return RedirectResponse(
            f"/xero?error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
def api_xero_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    return_to = "/xero"
    user_id: int | None = None
    if state:
        try:
            parsed = parse_oauth_state(state)
            return_to = parsed["return_to"]
            user_id = parsed["user_id"]
        except XeroAuthError:
            pass

    if error:
        detail = error_description or error
        return RedirectResponse(
            f"/xero?error={quote(detail)}",
            status_code=302,
        )

    if not code or user_id is None:
        return RedirectResponse(
            f"/xero?error={quote('Sign-in was interrupted. Please try again.')}",
            status_code=302,
        )

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return RedirectResponse(
            f"/xero?error={quote('Your dashboard login is no longer valid. Sign in and try again.')}",
            status_code=302,
        )

    try:
        with httpx.Client(timeout=60.0) as client:
            payload = exchange_authorization_code(client, code=code)
        save_tokens(
            db,
            refresh_token=str(payload.get("refresh_token") or ""),
            access_token=str(payload.get("access_token") or "") or None,
            expires_in=int(payload.get("expires_in") or 1800),
            user_id=user.id,
        )
        sync_organisations_from_xero(db)
    except (XeroAuthError, ValueError) as exc:
        return RedirectResponse(
            f"/xero?error={quote(str(exc))}",
            status_code=302,
        )

    separator = "&" if "?" in return_to else "?"
    return RedirectResponse(
        f"{return_to}{separator}xero=connected",
        status_code=302,
    )


@router.post("/organisations/sync")
def api_xero_sync_organisations(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    try:
        organisations = sync_organisations_from_xero(db)
    except XeroAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"organisations": organisations, "count": len(organisations)}


@router.put("/organisations/{tenant_id}")
def api_xero_map_organisation(
    tenant_id: str,
    body: OrganisationMapBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    try:
        organisation = set_dashboard_business(
            db,
            tenant_id=tenant_id,
            dashboard_business=body.dashboard_business,
        )
    except XeroAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return organisation


@router.post("/invoices/sync")
def api_xero_sync_invoices(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    try:
        result = sync_all_invoices(db)
    except XeroAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.get("/invoices/summary")
def api_xero_invoice_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    return invoice_summary(db)


@router.get("/actuals")
def api_xero_actuals(
    fiscal_year: int | None = None,
    business: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    years = available_actual_fiscal_years(db)
    year = fiscal_year if fiscal_year is not None else (years[0] if years else None)
    if year is None:
        raise HTTPException(status_code=400, detail="No invoice dates available yet.")
    return list_actuals(db, fiscal_year=year, business=business)


@router.get("/pnl")
def api_xero_pnl(
    fiscal_year: int | None = None,
    month_from: str | None = None,
    month_to: str | None = None,
    business: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    if month_from and month_to:
        try:
            start = dt.date.fromisoformat(month_from)
            end = dt.date.fromisoformat(month_to)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="month_from/month_to must be YYYY-MM-DD."
            ) from exc
        try:
            return list_xero_pnl(
                db, month_from=start, month_to=end, business=business
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    years = available_actual_fiscal_years(db)
    year = fiscal_year if fiscal_year is not None else (years[0] if years else None)
    if year is None:
        raise HTTPException(status_code=400, detail="No invoice dates available yet.")
    try:
        return list_xero_pnl(db, fiscal_year=year, business=business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/accounts/sync")
def api_xero_sync_accounts(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    try:
        return sync_all_accounts(db)
    except XeroAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/account-mappings")
def api_xero_list_account_mappings(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    return list_account_budget_mappings(db)


@router.put("/account-mappings/{tenant_id}/{account_id}")
def api_xero_set_account_mapping(
    tenant_id: str,
    account_id: str,
    body: AccountBudgetMapBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    try:
        return set_account_budget_mapping(
            db,
            tenant_id=tenant_id,
            account_id=account_id,
            mapping_id=body.mapping_id,
        )
    except XeroAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/disconnect")
def api_xero_disconnect(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_XERO)),
):
    clear_tokens(db)
    clear_invoices(db)
    clear_journals(db)
    clear_account_budget_mappings(db)
    clear_accounts(db)
    clear_organisations(db)
    return {"status": "disconnected"}
