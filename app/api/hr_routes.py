"""HR / Staff management API routes."""

from __future__ import annotations

import datetime as dt

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_action, require_page
from app.auth.permissions import (
    ACTION_HR_ENROLL,
    ACTION_HR_VIEW_SENSITIVE,
    PAGE_HR,
    has_action,
)
from app.db import get_db
from app.models import DOCUMENT_TYPE_OPTIONS, HR_BUSINESS_OPTIONS, PAY_TYPES, User
from app.services.contract_storage import ContractStorageError, default_storage
from app.services.docuseal_api import verify_webhook_secret
from app.services.hr_service import (
    HRServiceError,
    XLSX_CONTENT_TYPE,
    add_employee_document,
    add_job_title,
    build_staff_csv,
    build_staff_xlsx,
    delete_employee_document,
    enroll_employee,
    get_contract_for_download,
    get_document_for_download,
    get_staff_detail,
    handle_webhook,
    list_employee_contracts,
    list_employee_documents,
    list_job_titles,
    list_staff,
    list_templates,
    remove_job_title,
    save_draft,
    send_existing_employee,
    set_employee_archived,
    update_employee,
)

router = APIRouter(prefix="/api/hr")


class EnrollStaffBody(BaseModel):
    business: str = Field(min_length=2, max_length=64)
    employment_type: str = Field(default="employed", max_length=32)
    title: str | None = Field(default=None, max_length=16)
    employee_number: str | None = Field(default=None, max_length=64)
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=64)
    dob: dt.date | None = None
    address: str | None = None
    ni_number: str | None = Field(default=None, max_length=32)
    pay_type: str = Field(default="hourly")
    pay_rate: str | None = Field(default=None, max_length=64)
    role_title: str = Field(min_length=1, max_length=128)
    start_date: dt.date
    working_days_per_week: float | None = Field(default=None, ge=0, le=7)
    working_hours_per_day: float | None = Field(default=None, ge=0, le=24)
    driving_license_number: str | None = Field(default=None, max_length=64)
    license_points: str | None = Field(default=None, max_length=255)
    right_to_work_share_code: str | None = Field(default=None, max_length=64)
    bank_name: str | None = Field(default=None, max_length=128)
    account_holder_name: str | None = Field(default=None, max_length=128)
    sort_code: str | None = Field(default=None, max_length=16)
    account_number: str | None = Field(default=None, max_length=32)
    next_of_kin_name: str | None = Field(default=None, max_length=255)
    next_of_kin_relationship: str | None = Field(default=None, max_length=64)
    next_of_kin_phone: str | None = Field(default=None, max_length=64)
    template_id: int | None = None


class DraftStaffBody(BaseModel):
    """Relaxed body for saving a draft: only identity fields required, no template."""

    business: str = Field(min_length=2, max_length=64)
    employment_type: str = Field(default="employed", max_length=32)
    title: str | None = Field(default=None, max_length=16)
    employee_number: str | None = Field(default=None, max_length=64)
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=64)
    dob: dt.date | None = None
    address: str | None = None
    ni_number: str | None = Field(default=None, max_length=32)
    pay_type: str = Field(default="hourly")
    pay_rate: str | None = Field(default=None, max_length=64)
    role_title: str = Field(default="Farm Worker", min_length=1, max_length=128)
    start_date: dt.date
    working_days_per_week: float | None = Field(default=None, ge=0, le=7)
    working_hours_per_day: float | None = Field(default=None, ge=0, le=24)
    driving_license_number: str | None = Field(default=None, max_length=64)
    license_points: str | None = Field(default=None, max_length=255)
    right_to_work_share_code: str | None = Field(default=None, max_length=64)
    bank_name: str | None = Field(default=None, max_length=128)
    account_holder_name: str | None = Field(default=None, max_length=128)
    sort_code: str | None = Field(default=None, max_length=16)
    account_number: str | None = Field(default=None, max_length=32)
    next_of_kin_name: str | None = Field(default=None, max_length=255)
    next_of_kin_relationship: str | None = Field(default=None, max_length=64)
    next_of_kin_phone: str | None = Field(default=None, max_length=64)
    template_id: int | None = None


class SendStaffBody(BaseModel):
    template_id: int | None = None


class JobTitleBody(BaseModel):
    title: str = Field(min_length=1, max_length=128)


@router.get("/job-titles")
def api_list_job_titles(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    return {"titles": list_job_titles(db)}


@router.post("/job-titles")
def api_add_job_title(
    body: JobTitleBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return {"titles": add_job_title(db, body.title)}
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/job-titles")
def api_remove_job_title(
    body: JobTitleBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return {"titles": remove_job_title(db, body.title)}
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/staff")
def api_list_staff(
    search: str | None = Query(None),
    status: str | None = Query(None),
    view: str = Query("current"),
    business: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    if view not in ("current", "archived"):
        raise HTTPException(status_code=400, detail="Invalid view.")
    return {
        "staff": list_staff(
            db,
            search=search,
            status=status,
            view=view,
            business=business,
        )
    }


@router.get("/staff/export.csv")
def api_staff_export_csv(
    search: str | None = Query(None),
    view: str = Query("current"),
    business: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    if view not in ("current", "archived"):
        raise HTTPException(status_code=400, detail="Invalid view.")
    rows = list_staff(db, search=search, view=view, business=business)
    content = build_staff_csv(rows)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="staff_directory.csv"'},
    )


@router.get("/staff/export.xlsx")
def api_staff_export_xlsx(
    search: str | None = Query(None),
    view: str = Query("current"),
    business: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    if view not in ("current", "archived"):
        raise HTTPException(status_code=400, detail="Invalid view.")
    rows = list_staff(db, search=search, view=view, business=business)
    content = build_staff_xlsx(rows)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="staff_directory.xlsx"'
        },
    )


@router.get("/staff/{employee_id}")
def api_get_staff(
    employee_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_page(PAGE_HR)),
):
    include_sensitive = has_action(user, ACTION_HR_VIEW_SENSITIVE)
    try:
        return get_staff_detail(
            db, employee_id, include_sensitive=include_sensitive
        )
    except HRServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/staff/{employee_id}/contracts")
def api_list_staff_contracts(
    employee_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    try:
        return {"contracts": list_employee_contracts(db, employee_id)}
    except HRServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/templates")
def api_list_templates(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    templates = list_templates(db)
    return {
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "docuseal_template_id": t.docuseal_template_id,
                "description": t.description,
            }
            for t in templates
        ]
    }


@router.post("/staff/enroll")
def api_enroll_staff(
    body: EnrollStaffBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    if body.pay_type not in PAY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid pay_type.")
    if body.business not in HR_BUSINESS_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid business.")
    try:
        return enroll_employee(db, body.model_dump(), user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/staff/draft")
def api_save_draft(
    body: DraftStaffBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    if body.pay_type not in PAY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid pay_type.")
    if body.business not in HR_BUSINESS_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid business.")
    try:
        return save_draft(db, body.model_dump(), user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/staff/{employee_id}")
def api_update_staff(
    employee_id: int,
    body: DraftStaffBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    if body.pay_type not in PAY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid pay_type.")
    if body.business not in HR_BUSINESS_OPTIONS:
        raise HTTPException(status_code=400, detail="Invalid business.")
    try:
        return update_employee(db, employee_id, body.model_dump(), user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/staff/{employee_id}/send")
def api_send_staff(
    employee_id: int,
    body: SendStaffBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return send_existing_employee(db, employee_id, body.template_id, user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/staff/{employee_id}/archive")
def api_archive_staff(
    employee_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return set_employee_archived(db, employee_id, archived=True, user=user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/staff/{employee_id}/restore")
def api_restore_staff(
    employee_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return set_employee_archived(db, employee_id, archived=False, user=user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/contracts/{contract_id}/download")
def api_download_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_HR_VIEW_SENSITIVE)),
):
    try:
        contract = get_contract_for_download(db, contract_id)
        path = default_storage.resolve_download_path(contract.signed_pdf_path)
    except (HRServiceError, ContractStorageError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"contract_{contract.employee_id}_{contract_id}.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
    )


@router.get("/document-types")
def api_list_document_types(
    _: User = Depends(require_page(PAGE_HR)),
):
    return {"document_types": list(DOCUMENT_TYPE_OPTIONS)}


@router.get("/staff/{employee_id}/documents")
def api_list_staff_documents(
    employee_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_HR)),
):
    try:
        return {"documents": list_employee_documents(db, employee_id)}
    except HRServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/staff/{employee_id}/documents")
async def api_upload_staff_document(
    employee_id: int,
    file: UploadFile = File(...),
    doc_type: str = Form("Other"),
    label: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    content = await file.read()
    try:
        return add_employee_document(
            db,
            employee_id,
            doc_type=doc_type,
            label=label,
            filename=file.filename or "document",
            content=content,
            content_type=file.content_type,
            user=user,
        )
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents/{document_id}/download")
def api_download_document(
    document_id: int,
    inline: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_HR_VIEW_SENSITIVE)),
):
    try:
        document = get_document_for_download(db, document_id)
        path = default_storage.resolve_document_path(document.stored_path)
    except (HRServiceError, ContractStorageError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=document.content_type or "application/octet-stream",
        filename=document.original_filename or f"document_{document_id}",
        content_disposition_type="inline" if inline else "attachment",
    )


@router.delete("/documents/{document_id}")
def api_delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_HR_ENROLL)),
):
    try:
        return delete_employee_document(db, document_id, user)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/webhook")
async def api_docuseal_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
):
    query_secret = request.query_params.get("token")
    if not verify_webhook_secret(
        header_secret=x_webhook_secret,
        query_secret=query_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")

    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc

    try:
        result = handle_webhook(db, event)
    except HRServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
