"""HR staff enrollment and DocuSeal webhook orchestration."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import hr_team_emails_for
from app.models import (
    CONTRACT_STATUS_COMPLETED,
    CONTRACT_STATUS_PENDING,
    DOCUMENT_TYPE_OPTIONS,
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ARCHIVED,
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
    PAY_TYPES,
    ContractTemplate,
    Employee,
    EmployeeContract,
    EmployeeDocument,
    User,
)
from app.services.contract_storage import ContractStorageError, default_storage
from app.services.crypto_fields import decrypt_field, encrypt_field
from app.services.docuseal_api import DocuSealError, create_submission, download_signed_pdf

logger = logging.getLogger(__name__)


class HRServiceError(Exception):
    """HR operation failed."""


def parse_hr_team_emails(business: str | None = None) -> list[str]:
    raw = hr_team_emails_for(business)
    return [e.strip() for e in raw.split(",") if e.strip()]


def list_templates(db: Session, *, active_only: bool = True) -> list[ContractTemplate]:
    query = select(ContractTemplate).order_by(ContractTemplate.name)
    if active_only:
        query = query.where(ContractTemplate.is_active.is_(True))
    return list(db.scalars(query).all())


def list_staff(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    business: str | None = None,
) -> list[dict[str, Any]]:
    query = select(Employee).order_by(Employee.full_name)
    if status:
        query = query.where(Employee.status == status)
    if business:
        query = query.where(Employee.business == business)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(
            or_(
                Employee.full_name.ilike(term),
                Employee.email.ilike(term),
                Employee.role_title.ilike(term),
            )
        )
    rows = db.scalars(query).all()
    return [_employee_summary(row) for row in rows]


def get_staff_detail(
    db: Session,
    employee_id: int,
    *,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")
    return _employee_detail(employee, include_sensitive=include_sensitive)


def list_employee_contracts(db: Session, employee_id: int) -> list[dict[str, Any]]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")
    return [_contract_dict(c) for c in employee.contracts]


def _build_employee(
    payload: dict[str, Any],
    user: User,
    *,
    pay_type: str,
    template_id: int | None,
    status: str,
) -> Employee:
    def _clean(key: str) -> str | None:
        return (payload.get(key) or "").strip() or None

    return Employee(
        business=_clean("business"),
        title=_clean("title"),
        full_name=payload["full_name"].strip(),
        email=payload["email"].strip().lower(),
        phone=_clean("phone"),
        dob=payload.get("dob"),
        address=_clean("address"),
        ni_number_enc=encrypt_field(payload.get("ni_number")),
        pay_type=pay_type,
        pay_rate_enc=encrypt_field(payload.get("pay_rate")),
        role_title=payload["role_title"].strip(),
        start_date=payload["start_date"],
        working_days_per_week=payload.get("working_days_per_week"),
        working_hours_per_day=payload.get("working_hours_per_day"),
        driving_license_number_enc=encrypt_field(payload.get("driving_license_number")),
        license_points=_clean("license_points"),
        right_to_work_share_code=_clean("right_to_work_share_code"),
        bank_name=_clean("bank_name"),
        account_holder_name=_clean("account_holder_name"),
        sort_code_enc=encrypt_field(payload.get("sort_code")),
        account_number_enc=encrypt_field(payload.get("account_number")),
        next_of_kin_name=_clean("next_of_kin_name"),
        next_of_kin_relationship=_clean("next_of_kin_relationship"),
        next_of_kin_phone=_clean("next_of_kin_phone"),
        status=status,
        template_id=template_id,
        created_by_user_id=user.id,
    )


def _titlecase(value: Any) -> str:
    """Tidy capitalisation: first letter of each word upper, the rest lower
    (so JOHN SMITH and john smith both become John Smith). Also capitalises
    after hyphens and apostrophes (e.g. Anne-Marie, O'Brien)."""
    text_value = str(value or "").strip()
    if not text_value:
        return ""

    def cap_word(word: str) -> str:
        result = []
        capitalise_next = True
        for ch in word:
            if capitalise_next and ch.isalpha():
                result.append(ch.upper())
                capitalise_next = False
            else:
                result.append(ch.lower())
            if ch in "-'":
                capitalise_next = True
        return "".join(result)

    return " ".join(cap_word(word) for word in text_value.split())


def _format_date(value: Any) -> str:
    """Format a date as DD/MM/YYYY."""
    if value is None:
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _build_docuseal_data(
    employee: Employee, sensitive: dict[str, Any]
) -> dict[str, Any]:
    def _num(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    full_name_with_title = " ".join(
        part for part in (employee.title, _titlecase(employee.full_name)) if part
    )
    days = employee.working_days_per_week
    hours = employee.working_hours_per_day
    weekly_hours = _num(days * hours) if days is not None and hours is not None else ""

    return {
        "employee_id": employee.id,
        "business": employee.business or "",
        "title": employee.title or "",
        "full_name": full_name_with_title,
        "email": (employee.email or "").lower(),
        "phone": employee.phone or "",
        "dob": _format_date(employee.dob),
        "address": _titlecase(employee.address),
        "ni_number": (sensitive.get("ni_number") or "").upper(),
        "pay_type": _titlecase(employee.pay_type),
        "pay_rate": sensitive.get("pay_rate") or "",
        "start_date": _format_date(employee.start_date),
        "role_title": _titlecase(employee.role_title),
        "working_days_per_week": _num(employee.working_days_per_week),
        "working_hours_per_day": _num(employee.working_hours_per_day),
        "weekly_hours": weekly_hours,
        "date_today": _format_date(dt.date.today()),
        "driving_license_number": (sensitive.get("driving_license_number") or "").upper(),
        "license_points": employee.license_points or "",
        "right_to_work_share_code": (employee.right_to_work_share_code or "").upper(),
        "bank_name": _titlecase(employee.bank_name),
        "account_holder_name": _titlecase(employee.account_holder_name),
        "sort_code": sensitive.get("sort_code") or "",
        "account_number": sensitive.get("account_number") or "",
        "next_of_kin_name": _titlecase(employee.next_of_kin_name),
        "next_of_kin_relationship": _titlecase(employee.next_of_kin_relationship),
        "next_of_kin_phone": employee.next_of_kin_phone or "",
    }


def _submit_to_docuseal(
    db: Session,
    employee: Employee,
    template: ContractTemplate,
    sensitive: dict[str, Any],
) -> dict[str, Any]:
    reviewers = parse_hr_team_emails(employee.business)
    if not reviewers:
        raise HRServiceError(
            "No HR reviewer emails configured for this business. Set "
            "DOCUSEAL_CM_HR_TEAM_EMAILS / DOCUSEAL_GAD_HR_TEAM_EMAILS "
            "(or the global HR_HR_TEAM_EMAILS)."
        )

    employee_data = _build_docuseal_data(employee, sensitive)
    try:
        submission_id = create_submission(
            template_id=template.docuseal_template_id,
            employee_data=employee_data,
            reviewer_emails=reviewers,
            business=employee.business,
        )
    except DocuSealError as exc:
        db.rollback()
        raise HRServiceError(str(exc)) from exc

    contract = EmployeeContract(
        employee_id=employee.id,
        template_id=template.id,
        docuseal_submission_id=submission_id,
        status=CONTRACT_STATUS_PENDING,
    )
    db.add(contract)
    employee.status = EMPLOYEE_STATUS_PENDING_SIGNATURE
    db.commit()
    db.refresh(employee)

    logger.info(
        "Enrolled employee id=%s submission_id=%s", employee.id, submission_id
    )
    return {
        "employee": _employee_detail(employee, include_sensitive=False),
        "contract": _contract_dict(contract),
        "submission_id": submission_id,
    }


# Encrypted form field -> model attribute.
_ENCRYPTED_FIELDS = (
    ("ni_number", "ni_number_enc"),
    ("pay_rate", "pay_rate_enc"),
    ("sort_code", "sort_code_enc"),
    ("account_number", "account_number_enc"),
    ("driving_license_number", "driving_license_number_enc"),
)


def save_draft(
    db: Session,
    payload: dict[str, Any],
    user: User,
) -> dict[str, Any]:
    """Save a staff member as a draft (status=onboarding) without contacting DocuSeal."""
    pay_type = payload.get("pay_type", "hourly")
    if pay_type not in PAY_TYPES:
        raise HRServiceError(f"Invalid pay type: {pay_type}")

    template_id = payload.get("template_id")
    if template_id is not None:
        template = db.get(ContractTemplate, template_id)
        if template is None or not template.is_active:
            raise HRServiceError("Contract template not found or inactive.")

    employee = _build_employee(
        payload,
        user,
        pay_type=pay_type,
        template_id=template_id,
        status=EMPLOYEE_STATUS_ONBOARDING,
    )
    db.add(employee)
    db.commit()
    db.refresh(employee)
    logger.info("Saved draft employee id=%s", employee.id)
    return {"employee": _employee_detail(employee, include_sensitive=False)}


def enroll_employee(
    db: Session,
    payload: dict[str, Any],
    user: User,
) -> dict[str, Any]:
    template = db.get(ContractTemplate, payload["template_id"])
    if template is None or not template.is_active:
        raise HRServiceError("Contract template not found or inactive.")

    pay_type = payload.get("pay_type", "hourly")
    if pay_type not in PAY_TYPES:
        raise HRServiceError(f"Invalid pay type: {pay_type}")

    reviewers = parse_hr_team_emails(payload.get("business"))
    if not reviewers:
        raise HRServiceError(
            "No HR reviewer emails configured for this business. Set "
            "DOCUSEAL_CM_HR_TEAM_EMAILS / DOCUSEAL_GAD_HR_TEAM_EMAILS "
            "(or the global HR_HR_TEAM_EMAILS)."
        )

    employee = _build_employee(
        payload,
        user,
        pay_type=pay_type,
        template_id=template.id,
        status=EMPLOYEE_STATUS_ONBOARDING,
    )
    db.add(employee)
    db.flush()

    sensitive = {key: payload.get(key) for key, _ in _ENCRYPTED_FIELDS}
    return _submit_to_docuseal(db, employee, template, sensitive)


def update_employee(
    db: Session,
    employee_id: int,
    payload: dict[str, Any],
    user: User,
) -> dict[str, Any]:
    """Edit an employee's details (any status).

    Blank encrypted fields keep their existing values so an editor without
    sensitive-view access can update other details without wiping them.
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")

    pay_type = payload.get("pay_type", employee.pay_type)
    if pay_type not in PAY_TYPES:
        raise HRServiceError(f"Invalid pay type: {pay_type}")

    def _clean(key: str) -> str | None:
        return (payload.get(key) or "").strip() or None

    employee.business = _clean("business") or employee.business
    employee.title = _clean("title")
    employee.full_name = payload["full_name"].strip()
    employee.email = payload["email"].strip().lower()
    employee.phone = _clean("phone")
    employee.dob = payload.get("dob")
    employee.address = _clean("address")
    employee.pay_type = pay_type
    employee.role_title = payload["role_title"].strip()
    employee.start_date = payload["start_date"]
    employee.working_days_per_week = payload.get("working_days_per_week")
    employee.working_hours_per_day = payload.get("working_hours_per_day")
    employee.license_points = _clean("license_points")
    employee.right_to_work_share_code = _clean("right_to_work_share_code")
    employee.bank_name = _clean("bank_name")
    employee.account_holder_name = _clean("account_holder_name")
    employee.next_of_kin_name = _clean("next_of_kin_name")
    employee.next_of_kin_relationship = _clean("next_of_kin_relationship")
    employee.next_of_kin_phone = _clean("next_of_kin_phone")

    for form_key, attr in _ENCRYPTED_FIELDS:
        raw = payload.get(form_key)
        if raw is not None and str(raw).strip() != "":
            setattr(employee, attr, encrypt_field(str(raw)))

    if payload.get("template_id") is not None:
        template = db.get(ContractTemplate, payload["template_id"])
        if template is None or not template.is_active:
            raise HRServiceError("Contract template not found or inactive.")
        employee.template_id = template.id

    db.commit()
    db.refresh(employee)
    logger.info("Updated draft employee id=%s", employee.id)
    return {"employee": _employee_detail(employee, include_sensitive=False)}


def send_existing_employee(
    db: Session,
    employee_id: int,
    template_id: int | None,
    user: User,
) -> dict[str, Any]:
    """Send a contract to DocuSeal for signing.

    Works for a saved draft (first send) or an existing staff member (resend a
    new contract). Each call creates a fresh DocuSeal submission and contract
    record and moves the employee to 'pending signature'.
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")

    resolved_id = template_id or employee.template_id
    if resolved_id is None:
        raise HRServiceError("Select a contract template before sending.")
    template = db.get(ContractTemplate, resolved_id)
    if template is None or not template.is_active:
        raise HRServiceError("Contract template not found or inactive.")
    employee.template_id = template.id

    sensitive = {
        "ni_number": decrypt_field(employee.ni_number_enc),
        "pay_rate": decrypt_field(employee.pay_rate_enc),
        "sort_code": decrypt_field(employee.sort_code_enc),
        "account_number": decrypt_field(employee.account_number_enc),
        "driving_license_number": decrypt_field(employee.driving_license_number_enc),
    }
    return _submit_to_docuseal(db, employee, template, sensitive)


def set_employee_archived(
    db: Session,
    employee_id: int,
    *,
    archived: bool,
    user: User,
) -> dict[str, Any]:
    """Archive (remove from active employment) or restore an employee."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")

    if archived:
        employee.status = EMPLOYEE_STATUS_ARCHIVED
    else:
        if employee.status != EMPLOYEE_STATUS_ARCHIVED:
            raise HRServiceError("Only archived staff can be restored.")
        employee.status = EMPLOYEE_STATUS_ACTIVE

    db.commit()
    db.refresh(employee)
    logger.info(
        "%s employee id=%s by user=%s",
        "Archived" if archived else "Restored",
        employee.id,
        user.id,
    )
    return {"employee": _employee_detail(employee, include_sensitive=False)}


def handle_webhook(db: Session, event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event_type") or event.get("type") or ""
    if event_type not in ("submission.completed", "form.completed"):
        return {"status": "ignored", "event_type": event_type}

    data = event.get("data") or event
    submission_id = data.get("submission_id") or data.get("id")
    if submission_id is None:
        submission = data.get("submission") or {}
        submission_id = submission.get("id")
    if submission_id is None:
        raise HRServiceError("Webhook missing submission id.")

    submission_id = int(submission_id)
    contract = db.scalar(
        select(EmployeeContract).where(
            EmployeeContract.docuseal_submission_id == submission_id
        )
    )
    if contract is None:
        logger.warning("No contract for submission_id=%s", submission_id)
        return {"status": "unknown_submission", "submission_id": submission_id}

    if contract.status == CONTRACT_STATUS_COMPLETED and contract.signed_pdf_path:
        return {"status": "already_completed", "contract_id": contract.id}

    try:
        pdf_bytes = download_signed_pdf(submission_id)
        path, sha256 = default_storage.save_signed_pdf(
            employee_id=contract.employee_id,
            submission_id=submission_id,
            content=pdf_bytes,
        )
    except (DocuSealError, ContractStorageError) as exc:
        logger.exception("Failed to store signed PDF for submission %s", submission_id)
        raise HRServiceError(str(exc)) from exc

    contract.status = CONTRACT_STATUS_COMPLETED
    contract.signed_pdf_path = path
    contract.signed_pdf_sha256 = sha256
    contract.completed_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    employee = contract.employee
    if employee is not None:
        employee.status = EMPLOYEE_STATUS_ACTIVE

    db.commit()
    logger.info(
        "Contract completed id=%s employee_id=%s sha256=%s",
        contract.id,
        contract.employee_id,
        sha256,
    )
    return {
        "status": "completed",
        "contract_id": contract.id,
        "employee_id": contract.employee_id,
    }


# --- Staff documents ---

# Accept common identity-document formats. Keep PDFs and images only.
ALLOWED_DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/tiff",
}
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 20 MB


def list_employee_documents(db: Session, employee_id: int) -> list[dict[str, Any]]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")
    return [_document_dict(d) for d in employee.documents]


def add_employee_document(
    db: Session,
    employee_id: int,
    *,
    doc_type: str,
    label: str | None,
    filename: str,
    content: bytes,
    content_type: str | None,
    user: User,
) -> dict[str, Any]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HRServiceError("Employee not found.")

    if not content:
        raise HRServiceError("The uploaded file is empty.")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HRServiceError("File is too large (20 MB maximum).")

    ext = os.path.splitext(filename or "")[1].lower()
    type_ok = (content_type or "").lower() in ALLOWED_DOCUMENT_CONTENT_TYPES
    ext_ok = ext in ALLOWED_DOCUMENT_EXTENSIONS
    if not (type_ok or ext_ok):
        raise HRServiceError(
            "Unsupported file type. Upload a PDF or image (JPG, PNG, etc.)."
        )

    resolved_type = doc_type if doc_type in DOCUMENT_TYPE_OPTIONS else "Other"

    try:
        stored_path, sha256, size = default_storage.save_document(
            employee_id=employee.id,
            original_filename=filename or "document",
            content=content,
        )
    except ContractStorageError as exc:
        raise HRServiceError(str(exc)) from exc

    document = EmployeeDocument(
        employee_id=employee.id,
        doc_type=resolved_type,
        label=(label or "").strip() or None,
        original_filename=(filename or "document")[:255],
        content_type=content_type,
        file_size=size,
        stored_path=stored_path,
        sha256=sha256,
        uploaded_by_user_id=user.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    logger.info(
        "Added document id=%s employee_id=%s type=%s",
        document.id,
        employee.id,
        resolved_type,
    )
    return {"document": _document_dict(document)}


def get_document_for_download(db: Session, document_id: int) -> EmployeeDocument:
    document = db.get(EmployeeDocument, document_id)
    if document is None:
        raise HRServiceError("Document not found.")
    return document


def delete_employee_document(
    db: Session, document_id: int, user: User
) -> dict[str, Any]:
    document = db.get(EmployeeDocument, document_id)
    if document is None:
        raise HRServiceError("Document not found.")
    stored_path = document.stored_path
    employee_id = document.employee_id
    db.delete(document)
    db.commit()
    try:
        default_storage.delete_document(stored_path)
    except ContractStorageError:
        logger.warning("Document record deleted but file remained: %s", stored_path)
    logger.info(
        "Deleted document id=%s employee_id=%s by user=%s",
        document_id,
        employee_id,
        user.id,
    )
    return {"status": "deleted", "document_id": document_id}


def _document_dict(document: EmployeeDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "employee_id": document.employee_id,
        "doc_type": document.doc_type,
        "label": document.label,
        "original_filename": document.original_filename,
        "content_type": document.content_type,
        "file_size": document.file_size,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


def get_contract_for_download(db: Session, contract_id: int) -> EmployeeContract:
    contract = db.get(EmployeeContract, contract_id)
    if contract is None:
        raise HRServiceError("Contract not found.")
    if contract.status != CONTRACT_STATUS_COMPLETED or not contract.signed_pdf_path:
        raise HRServiceError("Signed contract is not available yet.")
    return contract


def _employee_summary(employee: Employee) -> dict[str, Any]:
    return {
        "id": employee.id,
        "business": employee.business,
        "title": employee.title,
        "full_name": employee.full_name,
        "email": employee.email,
        "role_title": employee.role_title,
        "start_date": employee.start_date.isoformat(),
        "status": employee.status,
        "phone": employee.phone,
    }


def _employee_detail(employee: Employee, *, include_sensitive: bool) -> dict[str, Any]:
    out = _employee_summary(employee)
    out.update(
        {
            "dob": employee.dob.isoformat() if employee.dob else None,
            "address": employee.address,
            "pay_type": employee.pay_type,
            "working_days_per_week": employee.working_days_per_week,
            "working_hours_per_day": employee.working_hours_per_day,
            "license_points": employee.license_points,
            "right_to_work_share_code": employee.right_to_work_share_code,
            "bank_name": employee.bank_name,
            "account_holder_name": employee.account_holder_name,
            "next_of_kin_name": employee.next_of_kin_name,
            "next_of_kin_relationship": employee.next_of_kin_relationship,
            "next_of_kin_phone": employee.next_of_kin_phone,
            "template_id": employee.template_id,
            "created_at": employee.created_at.isoformat() if employee.created_at else None,
            "updated_at": employee.updated_at.isoformat() if employee.updated_at else None,
            "contracts": [_contract_dict(c) for c in employee.contracts],
            "documents": [_document_dict(d) for d in employee.documents],
        }
    )
    sensitive_enc = (
        employee.ni_number_enc,
        employee.pay_rate_enc,
        employee.driving_license_number_enc,
        employee.sort_code_enc,
        employee.account_number_enc,
    )
    if include_sensitive:
        out["ni_number"] = decrypt_field(employee.ni_number_enc)
        out["pay_rate"] = decrypt_field(employee.pay_rate_enc)
        out["driving_license_number"] = decrypt_field(employee.driving_license_number_enc)
        out["sort_code"] = decrypt_field(employee.sort_code_enc)
        out["account_number"] = decrypt_field(employee.account_number_enc)
    else:
        for key in (
            "ni_number",
            "pay_rate",
            "driving_license_number",
            "sort_code",
            "account_number",
        ):
            out[key] = None
        out["has_sensitive_data"] = any(sensitive_enc)
    return out


def _contract_dict(contract: EmployeeContract) -> dict[str, Any]:
    return {
        "id": contract.id,
        "employee_id": contract.employee_id,
        "template_id": contract.template_id,
        "docuseal_submission_id": contract.docuseal_submission_id,
        "status": contract.status,
        "signed_pdf_sha256": contract.signed_pdf_sha256,
        "has_signed_pdf": bool(contract.signed_pdf_path),
        "completed_at": contract.completed_at.isoformat() if contract.completed_at else None,
        "created_at": contract.created_at.isoformat() if contract.created_at else None,
    }
