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
from app.services.benchmarking_farm_rations import (
    create_farm_ration,
    deactivate_farm_ration,
    get_farm_ration_workbook,
    get_ration_cost_comparison,
    normalize_farm_code,
    save_farm_ration_inclusions,
    update_farm_ration,
)
from app.services.benchmarking_rations import (
    create_ingredient,
    deactivate_ingredient,
    list_ingredient_categories,
    list_ingredient_costs,
    list_ingredients,
    save_ingredient_costs,
    update_ingredient,
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


class CreateIngredientBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str


class UpdateIngredientBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str


class IngredientCostRowBody(BaseModel):
    cost_month: dt.date
    ingredient_id: int
    cost: float | None = None


class SaveIngredientCostsBody(BaseModel):
    fiscal_year: int
    rows: list[IngredientCostRowBody] = Field(default_factory=list)


@router.get("/rations/categories")
def api_ration_categories(
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"categories": list_ingredient_categories()}


@router.get("/rations/ingredients")
def api_list_ration_ingredients(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"ingredients": list_ingredients(db)}


@router.post("/rations/ingredients")
def api_create_ration_ingredient(
    body: CreateIngredientBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        ingredient = create_ingredient(
            db,
            name=body.name,
            category=body.category,
            user_id=user.id,
        )
        return {"ingredient": ingredient}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/rations/ingredients/{ingredient_id}")
def api_update_ration_ingredient(
    ingredient_id: int,
    body: UpdateIngredientBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        ingredient = update_ingredient(
            db,
            ingredient_id=ingredient_id,
            name=body.name,
            category=body.category,
        )
        return {"ingredient": ingredient}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/rations/ingredients/{ingredient_id}")
def api_deactivate_ration_ingredient(
    ingredient_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        deactivate_ingredient(db, ingredient_id=ingredient_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rations/ingredient-costs")
def api_list_ingredient_costs(
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
    return list_ingredient_costs(db, fiscal_year=year)


@router.put("/rations/ingredient-costs")
def api_save_ingredient_costs(
    body: SaveIngredientCostsBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    years = available_fiscal_years()
    if body.fiscal_year not in years:
        raise HTTPException(
            status_code=400,
            detail=f"fiscal_year must be one of {years}",
        )
    return save_ingredient_costs(
        db,
        fiscal_year=body.fiscal_year,
        rows=[row.model_dump() for row in body.rows],
        user_id=user.id,
    )


class FarmRationBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    ingredient_ids: list[int] = Field(min_length=1)


class FarmRationInclusionRowBody(BaseModel):
    inclusion_month: dt.date
    ingredient_id: int
    kg_per_head: float | None = None


class SaveFarmRationInclusionsBody(BaseModel):
    fiscal_year: int
    rows: list[FarmRationInclusionRowBody] = Field(default_factory=list)


def _validate_farm_slug(farm: str) -> str:
    try:
        return normalize_farm_code(farm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rations/farms/{farm}")
def api_get_farm_rations(
    farm: str,
    fiscal_year: int | None = Query(None),
    ration_id: int | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    _validate_farm_slug(farm)
    years = available_fiscal_years()
    year = fiscal_year if fiscal_year is not None else years[0]
    if year not in years:
        raise HTTPException(
            status_code=400,
            detail=f"fiscal_year must be one of {years}",
        )
    return get_farm_ration_workbook(
        db, farm=farm, fiscal_year=year, ration_id=ration_id
    )


@router.post("/rations/farms/{farm}")
def api_create_farm_ration(
    farm: str,
    body: FarmRationBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    _validate_farm_slug(farm)
    try:
        ration = create_farm_ration(
            db,
            farm=farm,
            name=body.name,
            ingredient_ids=body.ingredient_ids,
            user_id=user.id,
        )
        return {"ration": ration}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/rations/farms/{farm}/{ration_id}")
def api_update_farm_ration(
    farm: str,
    ration_id: int,
    body: FarmRationBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    _validate_farm_slug(farm)
    try:
        ration = update_farm_ration(
            db,
            ration_id=ration_id,
            farm=farm,
            name=body.name,
            ingredient_ids=body.ingredient_ids,
        )
        return {"ration": ration}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/rations/farms/{farm}/{ration_id}")
def api_deactivate_farm_ration(
    farm: str,
    ration_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    _validate_farm_slug(farm)
    try:
        deactivate_farm_ration(db, ration_id=ration_id, farm=farm)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/rations/farms/{farm}/{ration_id}/inclusions")
def api_save_farm_ration_inclusions(
    farm: str,
    ration_id: int,
    body: SaveFarmRationInclusionsBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    _validate_farm_slug(farm)
    years = available_fiscal_years()
    if body.fiscal_year not in years:
        raise HTTPException(
            status_code=400,
            detail=f"fiscal_year must be one of {years}",
        )
    try:
        return save_farm_ration_inclusions(
            db,
            farm=farm,
            ration_id=ration_id,
            fiscal_year=body.fiscal_year,
            rows=[row.model_dump() for row in body.rows],
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rations/cost-comparison")
def api_ration_cost_comparison(
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
    return get_ration_cost_comparison(db, fiscal_year=year)
