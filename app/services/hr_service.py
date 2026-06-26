"""HR staff enrollment and DocuSeal webhook orchestration."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import HR_HR_TEAM_EMAILS
from app.models import (
    CONTRACT_STATUS_COMPLETED,
    CONTRACT_STATUS_PENDING,
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
    PAY_TYPES,
    ContractTemplate,
    Employee,
    EmployeeContract,
    User,
)
from app.services.contract_storage import ContractStorageError, default_storage
from app.services.crypto_fields import decrypt_field, encrypt_field
from app.services.docuseal_api import DocuSealError, create_submission, download_signed_pdf

logger = logging.getLogger(__name__)


class HRServiceError(Exception):
    """HR operation failed."""


def parse_hr_team_emails() -> list[str]:
    return [e.strip() for e in HR_HR_TEAM_EMAILS.split(",") if e.strip()]


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
) -> list[dict[str, Any]]:
    query = select(Employee).order_by(Employee.full_name)
    if status:
        query = query.where(Employee.status == status)
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

    reviewers = parse_hr_team_emails()
    if not reviewers:
        raise HRServiceError(
            "HR_HR_TEAM_EMAILS is not configured (comma-separated reviewer emails)."
        )

    def _clean(key: str) -> str | None:
        return (payload.get(key) or "").strip() or None

    employee = Employee(
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
        status=EMPLOYEE_STATUS_ONBOARDING,
        template_id=template.id,
        created_by_user_id=user.id,
    )
    db.add(employee)
    db.flush()

    def _num(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    full_name_with_title = " ".join(
        part for part in (employee.title, employee.full_name) if part
    )

    days = employee.working_days_per_week
    hours = employee.working_hours_per_day
    weekly_hours = _num(days * hours) if days is not None and hours is not None else ""

    employee_data = {
        "employee_id": employee.id,
        "business": employee.business or "",
        "title": employee.title or "",
        "full_name": full_name_with_title,
        "email": employee.email,
        "phone": employee.phone or "",
        "dob": employee.dob.isoformat() if employee.dob else "",
        "address": employee.address or "",
        "ni_number": payload.get("ni_number") or "",
        "pay_type": employee.pay_type,
        "pay_rate": payload.get("pay_rate") or "",
        "start_date": employee.start_date.isoformat(),
        "role_title": employee.role_title,
        "working_days_per_week": _num(employee.working_days_per_week),
        "working_hours_per_day": _num(employee.working_hours_per_day),
        "weekly_hours": weekly_hours,
        "date_today": dt.date.today().isoformat(),
        "driving_license_number": payload.get("driving_license_number") or "",
        "license_points": employee.license_points or "",
        "right_to_work_share_code": employee.right_to_work_share_code or "",
        "bank_name": employee.bank_name or "",
        "account_holder_name": employee.account_holder_name or "",
        "sort_code": payload.get("sort_code") or "",
        "account_number": payload.get("account_number") or "",
        "next_of_kin_name": employee.next_of_kin_name or "",
        "next_of_kin_relationship": employee.next_of_kin_relationship or "",
        "next_of_kin_phone": employee.next_of_kin_phone or "",
    }

    try:
        submission_id = create_submission(
            template_id=template.docuseal_template_id,
            employee_data=employee_data,
            reviewer_emails=reviewers,
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
        "Enrolled employee id=%s submission_id=%s",
        employee.id,
        submission_id,
    )
    return {
        "employee": _employee_detail(employee, include_sensitive=False),
        "contract": _contract_dict(contract),
        "submission_id": submission_id,
    }


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
