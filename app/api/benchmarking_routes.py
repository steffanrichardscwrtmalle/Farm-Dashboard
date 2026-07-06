"""Benchmarking API routes."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import ACTION_BENCHMARKING_EDIT, PAGE_BENCHMARKING
from app.db import get_db
from app.models import User
from app.services.benchmarking import (
    available_fiscal_years,
    list_forecasts,
    list_metric_definitions,
    save_forecasts,
)

router = APIRouter(prefix="/api/benchmarking")


class ForecastRowBody(BaseModel):
    forecast_month: dt.date
    farm: str
    quantity: float | None = None
    unit_price: float | None = None


class SaveForecastsBody(BaseModel):
    fiscal_year: int
    metric: str
    rows: list[ForecastRowBody] = Field(default_factory=list)


@router.get("/forecasts/metrics")
def api_benchmarking_metrics(
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"metrics": list_metric_definitions()}


@router.get("/forecasts")
def api_list_forecasts(
    fiscal_year: int | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    years = available_fiscal_years()
    year = fiscal_year if fiscal_year is not None else years[0]
    if year not in years:
        raise HTTPException(
            status_code=400,
            detail=f"fiscal_year must be one of {years}",
        )
    return list_forecasts(db, fiscal_year=year)


@router.put("/forecasts")
def api_save_forecasts(
    body: SaveForecastsBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    years = available_fiscal_years()
    if body.fiscal_year not in years:
        raise HTTPException(
            status_code=400,
            detail=f"fiscal_year must be one of {years}",
        )
    try:
        return save_forecasts(
            db,
            fiscal_year=body.fiscal_year,
            metric=body.metric,
            rows=[row.model_dump() for row in body.rows],
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
