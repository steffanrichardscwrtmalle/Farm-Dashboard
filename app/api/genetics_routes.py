"""Genetics API routes (pedigree registrations)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import (
    ACTION_GENETICS_PEDIGREE,
    ACTION_GENETICS_PENDING_RESULTS,
    PAGE_GENETICS,
)
from app.db import get_db
from app.models import User
from app.services.graph_email import GraphEmailError, send_mail_with_attachment
from app.services.genomic_progress import (
    build_genomic_progress,
    build_genomic_scatter,
    list_traits,
)
from app.services.pedigree_registrations import (
    build_pedigree_csv,
    get_recipient,
    list_pedigree_registrations,
    mark_registered,
    restore_registrations,
    set_recipient,
)
from app.services.sire_conflicts import build_sire_conflicts_csv, list_sire_conflicts
from app.services.ahdb_bulls import AhdbBullsError, ensure_imported, list_bulls, refresh_bulls
from app.services.custom_indexes import reset_index_settings, save_index_settings
from app.services.pending_results import (
    EMAIL_BODY as PENDING_EMAIL_BODY,
    XLSX_CONTENT_TYPE,
    build_pending_results_xlsx,
    get_recipient as get_pending_recipient,
    list_pending_results,
    set_recipient as set_pending_recipient,
)

router = APIRouter(prefix="/api/genetics")


class PedigreeKeyItem(BaseModel):
    farm: str
    etag: str


class PedigreeBulkBody(BaseModel):
    items: list[PedigreeKeyItem] = Field(default_factory=list)


class PedigreeEmailBody(BaseModel):
    recipient: EmailStr
    items: list[PedigreeKeyItem] = Field(default_factory=list)


class RecipientBody(BaseModel):
    recipient: EmailStr


@router.get("/pedigree-registrations")
def api_pedigree_registrations(
    status: str = Query("active", pattern="^(active|registered)$"),
    farm: list[str] | None = Query(None),
    sreg: str = Query("with", pattern="^(with|without|all)$"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return list_pedigree_registrations(db, status=status, farms=farm, sreg=sreg)


@router.get("/pedigree-registrations/export.csv")
def api_pedigree_registrations_export_csv(
    status: str = Query("active", pattern="^(active|registered)$"),
    farm: list[str] | None = Query(None),
    sreg: str = Query("with", pattern="^(with|without|all)$"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    result = list_pedigree_registrations(db, status=status, farms=farm, sreg=sreg)
    content = build_pedigree_csv(result["rows"])
    filename = f"pedigree_registrations_{status}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/pedigree-registrations/recipient")
def api_pedigree_recipient_get(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return get_recipient(db)


@router.put("/pedigree-registrations/recipient")
def api_pedigree_recipient_put(
    body: RecipientBody,
    db: Session = Depends(get_db),
    _user: User = Depends(require_action(ACTION_GENETICS_PEDIGREE)),
):
    try:
        return set_recipient(db, body.recipient)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pedigree-registrations/email")
def api_pedigree_email(
    body: PedigreeEmailBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_GENETICS_PEDIGREE)),
):
    items = [item.model_dump() for item in body.items]
    if not items:
        raise HTTPException(status_code=400, detail="Select at least one animal.")

    active = list_pedigree_registrations(db, status="active", farms=None, sreg="all")
    allowed = {
        (row["farm"], row["etag"])
        for row in active["rows"]
    }
    for item in items:
        if (item["farm"], item["etag"]) not in allowed:
            raise HTTPException(
                status_code=400,
                detail="One or more animals are not eligible for registration.",
            )

    export_rows = [
        row
        for row in active["rows"]
        if (row["farm"], row["etag"]) in {(i["farm"], i["etag"]) for i in items}
    ]
    csv_bytes = build_pedigree_csv(export_rows)
    today = dt.date.today().isoformat()
    filename = f"pedigree_registrations_{today}.csv"
    subject = f"Pedigree registration — {len(export_rows)} animals ({today})"
    message_body = (
        f"Please find attached the pedigree registration list for {len(export_rows)} "
        f"animals exported from the farm dashboard on {today}."
    )

    try:
        send_mail_with_attachment(
            to=body.recipient,
            subject=subject,
            body=message_body,
            filename=filename,
            content_bytes=csv_bytes,
        )
    except GraphEmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return mark_registered(db, items, user, emailed_to=body.recipient)


@router.post("/pedigree-registrations/restore")
def api_pedigree_restore(
    body: PedigreeBulkBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_GENETICS_PEDIGREE)),
):
    items = [item.model_dump() for item in body.items]
    return restore_registrations(db, items, user)


@router.get("/genomic-progress/traits")
def api_genomic_progress_traits(
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return {"traits": list_traits()}


@router.get("/genomic-progress")
def api_genomic_progress(
    trait: str = Query("pli"),
    farm: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    try:
        return build_genomic_progress(db, trait=trait, farms=farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/genomic-scatter")
def api_genomic_scatter(
    x_trait: str = Query("milk_kg"),
    y_trait: str = Query("pli"),
    farm: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    try:
        return build_genomic_scatter(db, x_trait=x_trait, y_trait=y_trait, farms=farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sire-conflicts")
def api_sire_conflicts(
    farm: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return list_sire_conflicts(db, farms=farm)


@router.get("/sire-conflicts/export.csv")
def api_sire_conflicts_export_csv(
    farm: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    result = list_sire_conflicts(db, farms=farm)
    content = build_sire_conflicts_csv(result["rows"])
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sire_conflicts.csv"'},
    )


class PendingRecipientBody(BaseModel):
    recipient: EmailStr


class PendingEmailBody(BaseModel):
    recipient: EmailStr


@router.get("/pending-results")
def api_pending_results(
    farm: list[str] | None = Query(None),
    min_days: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return list_pending_results(db, farms=farm, min_days=min_days)


@router.get("/pending-results/export.xlsx")
def api_pending_results_export_xlsx(
    farm: list[str] | None = Query(None),
    min_days: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    result = list_pending_results(db, farms=farm, min_days=min_days)
    content = build_pending_results_xlsx(result["rows"])
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="pending_results.xlsx"'
        },
    )


@router.get("/pending-results/recipient")
def api_pending_recipient_get(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    return get_pending_recipient(db)


@router.put("/pending-results/recipient")
def api_pending_recipient_put(
    body: PendingRecipientBody,
    db: Session = Depends(get_db),
    _user: User = Depends(require_action(ACTION_GENETICS_PENDING_RESULTS)),
):
    try:
        return set_pending_recipient(db, body.recipient)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pending-results/email")
def api_pending_results_email(
    body: PendingEmailBody,
    farm: list[str] | None = Query(None),
    min_days: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_action(ACTION_GENETICS_PENDING_RESULTS)),
):
    result = list_pending_results(db, farms=farm, min_days=min_days)
    rows = result["rows"]
    if not rows:
        raise HTTPException(
            status_code=400, detail="There are no pending submissions to send."
        )

    xlsx_bytes = build_pending_results_xlsx(rows)
    today = dt.date.today().isoformat()
    filename = f"pending_genomic_submissions_{today}.xlsx"
    subject = f"Pending genomic submissions — {len(rows)} animals ({today})"

    try:
        send_mail_with_attachment(
            to=body.recipient,
            subject=subject,
            body=PENDING_EMAIL_BODY,
            filename=filename,
            content_bytes=xlsx_bytes,
            content_type=XLSX_CONTENT_TYPE,
        )
    except GraphEmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"sent": len(rows), "recipient": body.recipient}


@router.get("/bull-search")
def api_bull_search(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    try:
        return ensure_imported(db)
    except AhdbBullsError as exc:
        existing = list_bulls(db)
        if existing["count"]:
            existing["warning"] = str(exc)
            return existing
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/bull-search/refresh")
def api_bull_search_refresh(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    try:
        return refresh_bulls(db)
    except AhdbBullsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class IndexSchemeBody(BaseModel):
    fat_pct_base: float | None = None
    protein_pct_base: float | None = None
    milk_volume_base: float | None = None
    fat_price: float | None = None
    protein_price: float | None = None
    volume_price: float | None = None
    lameness_weight: float | None = None
    include_lameness: bool | None = None


class IndexSettingsBody(BaseModel):
    ebv_conv: float | None = None
    fertility_weight: float | None = None
    lifespan_weight: float | None = None
    scc_value: float | None = None
    mastitis_weight: float | None = None
    include_mastitis: bool | None = None
    dp: IndexSchemeBody | None = None
    fw: IndexSchemeBody | None = None


@router.put("/bull-search/index-settings")
def api_bull_search_save_index_settings(
    body: IndexSettingsBody,
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    save_index_settings(db, body.model_dump(exclude_none=True))
    return list_bulls(db)


@router.post("/bull-search/index-settings/reset")
def api_bull_search_reset_index_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(require_page(PAGE_GENETICS)),
):
    reset_index_settings(db)
    return list_bulls(db)
