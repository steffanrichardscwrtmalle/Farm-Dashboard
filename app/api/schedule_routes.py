"""Farm Schedule API: recurring jobs, complete, archive."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_page
from app.auth.permissions import PAGE_SCHEDULE
from app.db import get_db
from app.models import User
from app.services.farm_schedule import (
    complete_job,
    create_job,
    deactivate_template,
    due_counts,
    list_schedule,
    normalize_farm,
    update_job,
)

router = APIRouter(prefix="/api/schedule")


class CreateJobBody(BaseModel):
    name: str
    due_date: str
    interval_days: int = Field(..., ge=1)
    notes: str | None = None


class CompleteJobBody(BaseModel):
    completed_on: str
    completed_by: str


class UpdateJobBody(BaseModel):
    name: str
    due_date: str
    interval_days: int = Field(..., ge=1)
    notes: str | None = None


def _farm_or_400(farm: str) -> str:
    try:
        return normalize_farm(farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/due-counts")
def api_due_counts(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    return due_counts(db)


@router.get("/{farm}")
def api_list_schedule(
    farm: str,
    view: str = "pending",
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    farm_key = _farm_or_400(farm)
    try:
        return list_schedule(db, farm=farm_key, view=view)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{farm}/jobs")
def api_create_job(
    farm: str,
    body: CreateJobBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    farm_key = _farm_or_400(farm)
    try:
        return create_job(
            db,
            farm=farm_key,
            name=body.name,
            due_date=body.due_date,
            interval_days=body.interval_days,
            notes=body.notes,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{farm}/jobs/{occurrence_id}")
def api_update_job(
    farm: str,
    occurrence_id: int,
    body: UpdateJobBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    farm_key = _farm_or_400(farm)
    try:
        return update_job(
            db,
            farm=farm_key,
            occurrence_id=occurrence_id,
            name=body.name,
            due_date=body.due_date,
            interval_days=body.interval_days,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{farm}/jobs/{occurrence_id}/complete")
def api_complete_job(
    farm: str,
    occurrence_id: int,
    body: CompleteJobBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    farm_key = _farm_or_400(farm)
    try:
        return complete_job(
            db,
            farm=farm_key,
            occurrence_id=occurrence_id,
            completed_on=body.completed_on,
            completed_by=body.completed_by,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{farm}/templates/{template_id}")
def api_deactivate_template(
    farm: str,
    template_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_SCHEDULE)),
) -> dict[str, Any]:
    farm_key = _farm_or_400(farm)
    try:
        return deactivate_template(db, farm=farm_key, template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
