"""Recurring farm maintenance jobs for the Schedule page."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
    Employee,
    FARM_JOB_STATUS_ARCHIVED,
    FARM_JOB_STATUS_PENDING,
    FarmJobOccurrence,
    FarmJobTemplate,
    HERD_FARM_OPTIONS,
)

FARM_LABELS: dict[str, str] = {
    "CM": "Cwrt Malle",
    "GAD": "Green Acre Dairy",
}
_FARM_BUSINESS: dict[str, str] = {
    "CM": "Cwrt Malle",
    "GAD": "Green Acre Dairy",
}
_STAFF_STATUSES = (
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
)


def normalize_farm(farm: str | None) -> str:
    value = (farm or "").strip().upper()
    if value not in HERD_FARM_OPTIONS:
        raise ValueError("Farm must be CM or GAD.")
    return value


def _parse_date(value: dt.date | str | None, *, field: str) -> dt.date:
    if value is None or value == "":
        raise ValueError(f"{field} is required.")
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid date.") from exc


def _serialize_occurrence(
    row: FarmJobOccurrence,
    *,
    as_of: dt.date,
    template: FarmJobTemplate | None = None,
) -> dict[str, Any]:
    tmpl = template or row.template
    days_until = (row.due_date - as_of).days
    return {
        "id": row.id,
        "template_id": row.template_id,
        "farm": row.farm,
        "name": tmpl.name if tmpl else "",
        "notes": tmpl.notes if tmpl else "",
        "interval_days": tmpl.interval_days if tmpl else None,
        "due_date": row.due_date.isoformat(),
        "status": row.status,
        "days_until_due": days_until,
        "overdue": row.status == FARM_JOB_STATUS_PENDING and days_until < 0,
        "due_today": row.status == FARM_JOB_STATUS_PENDING and days_until == 0,
        "completed_on": row.completed_on.isoformat() if row.completed_on else None,
        "completed_by": row.completed_by or "",
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def list_schedule(
    db: Session,
    *,
    farm: str,
    view: str = "pending",
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    today = as_of or dt.date.today()
    view_key = (view or "pending").strip().lower()
    if view_key not in {"pending", "archive"}:
        raise ValueError("View must be pending or archive.")

    status = (
        FARM_JOB_STATUS_PENDING if view_key == "pending" else FARM_JOB_STATUS_ARCHIVED
    )
    stmt = (
        select(FarmJobOccurrence)
        .join(FarmJobTemplate)
        .where(
            FarmJobOccurrence.farm == farm_key,
            FarmJobOccurrence.status == status,
        )
    )
    if view_key == "pending":
        stmt = stmt.where(FarmJobTemplate.is_active.is_(True)).order_by(
            FarmJobOccurrence.due_date.asc(),
            FarmJobTemplate.name.asc(),
        )
    else:
        stmt = stmt.order_by(
            FarmJobOccurrence.completed_on.desc(),
            FarmJobOccurrence.id.desc(),
        )

    rows = db.scalars(stmt).unique().all()
    return {
        "farm": farm_key,
        "farm_label": FARM_LABELS[farm_key],
        "view": view_key,
        "as_of": today.isoformat(),
        "total": len(rows),
        "rows": [_serialize_occurrence(row, as_of=today) for row in rows],
        "staff_names": list_staff_names(db, farm_key),
    }


def due_counts(db: Session, *, as_of: dt.date | None = None) -> dict[str, Any]:
    """Pending jobs due today or earlier, per farm."""
    today = as_of or dt.date.today()
    stmt = (
        select(FarmJobOccurrence.farm, func.count(FarmJobOccurrence.id))
        .join(FarmJobTemplate)
        .where(
            FarmJobOccurrence.status == FARM_JOB_STATUS_PENDING,
            FarmJobTemplate.is_active.is_(True),
            FarmJobOccurrence.due_date <= today,
        )
        .group_by(FarmJobOccurrence.farm)
    )
    counts = {farm: 0 for farm in HERD_FARM_OPTIONS}
    for farm, n in db.execute(stmt):
        if farm in counts:
            counts[farm] = int(n)
    return {"as_of": today.isoformat(), "counts": counts}


def list_staff_names(db: Session, farm: str) -> list[str]:
    farm_key = normalize_farm(farm)
    business = _FARM_BUSINESS[farm_key]
    names = db.scalars(
        select(Employee.full_name)
        .where(
            Employee.business == business,
            Employee.status.in_(_STAFF_STATUSES),
        )
        .order_by(Employee.full_name.asc())
    ).all()
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(name)
    return out


def create_job(
    db: Session,
    *,
    farm: str,
    name: str,
    due_date: dt.date | str,
    interval_days: int,
    notes: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    title = (name or "").strip()
    if not title:
        raise ValueError("Job name is required.")
    if interval_days < 1:
        raise ValueError("Repeat interval must be at least 1 day.")
    first_due = _parse_date(due_date, field="Due date")
    template = FarmJobTemplate(
        farm=farm_key,
        name=title,
        interval_days=int(interval_days),
        notes=(notes or "").strip(),
        created_by_user_id=user_id,
    )
    db.add(template)
    db.flush()
    occurrence = FarmJobOccurrence(
        template_id=template.id,
        farm=farm_key,
        due_date=first_due,
        status=FARM_JOB_STATUS_PENDING,
    )
    db.add(occurrence)
    db.commit()
    db.refresh(occurrence)
    return _serialize_occurrence(occurrence, as_of=dt.date.today(), template=template)


def update_job(
    db: Session,
    *,
    farm: str,
    occurrence_id: int,
    name: str,
    due_date: dt.date | str,
    interval_days: int,
    notes: str | None = None,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    row = db.get(FarmJobOccurrence, occurrence_id)
    if row is None or row.farm != farm_key:
        raise ValueError("Job not found.")
    if row.status != FARM_JOB_STATUS_PENDING:
        raise ValueError("Only current to-do jobs can be edited.")
    template = row.template
    if template is None or not template.is_active:
        raise ValueError("This schedule is no longer active.")
    title = (name or "").strip()
    if not title:
        raise ValueError("Job name is required.")
    if interval_days < 1:
        raise ValueError("Repeat interval must be at least 1 day.")
    row.due_date = _parse_date(due_date, field="Due date")
    template.name = title
    template.interval_days = int(interval_days)
    template.notes = (notes or "").strip()
    db.commit()
    db.refresh(row)
    return _serialize_occurrence(row, as_of=dt.date.today(), template=template)


def complete_job(
    db: Session,
    *,
    farm: str,
    occurrence_id: int,
    completed_on: dt.date | str,
    completed_by: str,
    user_id: int | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    row = db.get(FarmJobOccurrence, occurrence_id)
    if row is None or row.farm != farm_key:
        raise ValueError("Job not found.")
    if row.status != FARM_JOB_STATUS_PENDING:
        raise ValueError("This job is already completed.")
    template = row.template
    if template is None or not template.is_active:
        raise ValueError("This schedule is no longer active.")
    done_on = _parse_date(completed_on, field="Completed date")
    who = (completed_by or "").strip()
    if not who:
        raise ValueError("Who did it is required.")

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    row.status = FARM_JOB_STATUS_ARCHIVED
    row.completed_on = done_on
    row.completed_by = who
    row.completed_by_user_id = user_id
    row.completed_at = now

    next_due = done_on + dt.timedelta(days=template.interval_days)
    nxt = FarmJobOccurrence(
        template_id=template.id,
        farm=farm_key,
        due_date=next_due,
        status=FARM_JOB_STATUS_PENDING,
    )
    db.add(nxt)
    db.commit()
    db.refresh(row)
    db.refresh(nxt)
    today = as_of or dt.date.today()
    return {
        "completed": _serialize_occurrence(row, as_of=today, template=template),
        "next": _serialize_occurrence(nxt, as_of=today, template=template),
    }


def deactivate_template(
    db: Session,
    *,
    farm: str,
    template_id: int,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    template = db.get(FarmJobTemplate, template_id)
    if template is None or template.farm != farm_key:
        raise ValueError("Schedule not found.")
    template.is_active = False
    pending = db.scalars(
        select(FarmJobOccurrence).where(
            FarmJobOccurrence.template_id == template.id,
            FarmJobOccurrence.status == FARM_JOB_STATUS_PENDING,
        )
    ).all()
    removed = 0
    for row in pending:
        db.delete(row)
        removed += 1
    db.commit()
    return {"ok": True, "template_id": template.id, "removed_pending": removed}
