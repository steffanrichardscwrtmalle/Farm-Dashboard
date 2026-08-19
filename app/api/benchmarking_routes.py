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
from app.services.feed_purchase_forecasts import build_feed_purchase_forecasts_report
from app.services.financial_data_sources import list_financial_data_sources
from app.services.financial_forecast_autofill import fill_financial_forecasts_from_data_sources
from app.services.financial_forecasts import (
    add_financial_option,
    create_financial_mapping,
    delete_financial_mapping,
    delete_financial_option,
    list_band_definitions,
    list_financial_forecasts,
    list_financial_mappings,
    list_financial_options,
    save_financial_forecasts,
    update_financial_mapping,
    update_financial_option,
)
from app.services.hp_schedules import (
    build_hp_payment_chart,
    create_hp_schedule,
    deactivate_hp_schedule,
    list_hp_schedules,
    update_hp_schedule,
)
from app.services.milk_sales_forecasts import build_milk_sales_forecasts_report
from app.services.rental_agreements import (
    build_rental_agreements_report,
    create_rental_agreement,
    deactivate_rental_agreement,
    save_rental_payments,
    update_rental_agreement,
)
from app.services.stock_sales_purchases_forecasts import (
    build_stock_sales_purchases_forecasts_report,
)
from app.services.stock_forecasts import (
    build_stock_forecasts_page_report,
    build_stock_forecasts_report,
)
from app.services.stock_valuation_forecasts import build_stock_valuation_forecasts_report

router = APIRouter(prefix="/api/benchmarking")


class ForecastRowBody(BaseModel):
    forecast_month: dt.date
    farm: str
    quantity: float | None = None
    unit_price: float | None = None
    births: float | None = None


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


@router.get("/stock-forecasts-page")
def api_stock_forecasts_page(
    farm: list[str] | None = Query(None),
    stock_group: str = Query("cows", pattern="^(cows|youngstock|beef)$"),
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
    return build_stock_forecasts_page_report(
        db,
        farms=farm,
        stock_group=stock_group,
        fiscal_year=year,
    )


@router.get("/stock-forecasts")
def api_stock_forecasts(
    farm: list[str] | None = Query(None),
    stock_group: str = Query("cows", pattern="^(cows|youngstock|beef)$"),
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
    return build_stock_forecasts_report(
        db,
        farms=farm,
        stock_group=stock_group,
        fiscal_year=year,
    )


@router.get("/stock-valuation-forecasts")
def api_stock_valuation_forecasts(
    farm: list[str] | None = Query(None),
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
    return build_stock_valuation_forecasts_report(
        db,
        farms=farm,
        fiscal_year=year,
    )


@router.get("/feed-purchase-forecasts")
def api_feed_purchase_forecasts(
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
    return build_feed_purchase_forecasts_report(db, fiscal_year=year)


@router.get("/milk-sales-forecasts")
def api_milk_sales_forecasts(
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
    return build_milk_sales_forecasts_report(db, fiscal_year=year)


@router.get("/stock-sales-purchases-forecasts")
def api_stock_sales_purchases_forecasts(
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
    return build_stock_sales_purchases_forecasts_report(db, fiscal_year=year)


class FinancialOptionBody(BaseModel):
    option_type: str
    value: str = Field(min_length=1, max_length=255)


class FinancialOptionUpdateBody(BaseModel):
    value: str = Field(min_length=1, max_length=255)


class FinancialMappingBody(BaseModel):
    heading: str = Field(min_length=1, max_length=255)
    item_type: str = Field(min_length=1, max_length=64)
    band: str = Field(min_length=1, max_length=128)
    group: str = Field(min_length=1, max_length=128)
    data_sources: list[str] = Field(default_factory=list)


class FinancialForecastRowBody(BaseModel):
    mapping_id: int
    forecast_month: dt.date
    CM: float | None = None
    GAD: float | None = None


class SaveFinancialForecastsBody(BaseModel):
    fiscal_year: int
    band_id: str
    rows: list[FinancialForecastRowBody] = Field(default_factory=list)


class FillFinancialForecastsBody(BaseModel):
    fiscal_year: int
    farms: list[str] = Field(default_factory=list)
    fill_mode: str = "replace"


@router.get("/financial-forecasts/options")
def api_financial_forecast_options(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return list_financial_options(db)


@router.post("/financial-forecasts/options")
def api_add_financial_forecast_option(
    body: FinancialOptionBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        option = add_financial_option(db, body.option_type, body.value)
        return {"id": option.id, "option_type": option.option_type, "value": option.value}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/financial-forecasts/options/{option_id}")
def api_update_financial_forecast_option(
    option_id: int,
    body: FinancialOptionUpdateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        option = update_financial_option(db, option_id, body.value)
        return {"id": option.id, "option_type": option.option_type, "value": option.value}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/financial-forecasts/options/{option_id}")
def api_delete_financial_forecast_option(
    option_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        delete_financial_option(db, option_id)
        return {"deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/financial-forecasts/data-sources")
def api_financial_forecast_data_sources(
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return list_financial_data_sources()


@router.get("/financial-forecasts/mappings")
def api_financial_forecast_mappings(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"items": list_financial_mappings(db)}


@router.post("/financial-forecasts/mappings")
def api_create_financial_forecast_mapping(
    body: FinancialMappingBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        mapping = create_financial_mapping(
            db,
            heading=body.heading,
            item_type=body.item_type,
            band=body.band,
            group=body.group,
            data_sources=body.data_sources,
        )
        return {
            "id": mapping.id,
            "heading": mapping.heading,
            "item_type": mapping.item_type,
            "band": mapping.band,
            "group": mapping.group,
            "data_sources": body.data_sources,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface DB/schema errors as JSON
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not save mapping: {exc}") from exc


@router.put("/financial-forecasts/mappings/{mapping_id}")
def api_update_financial_forecast_mapping(
    mapping_id: int,
    body: FinancialMappingBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        mapping = update_financial_mapping(
            db,
            mapping_id,
            heading=body.heading,
            item_type=body.item_type,
            band=body.band,
            group=body.group,
            data_sources=body.data_sources,
        )
        sources = list_financial_mappings(db)
        row = next((item for item in sources if item["id"] == mapping.id), None)
        return {
            "id": mapping.id,
            "heading": mapping.heading,
            "item_type": mapping.item_type,
            "band": mapping.band,
            "group": mapping.group,
            "data_sources": row["data_sources"] if row else body.data_sources,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface DB/schema errors as JSON
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not save mapping: {exc}") from exc


@router.delete("/financial-forecasts/mappings/{mapping_id}")
def api_delete_financial_forecast_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        delete_financial_mapping(db, mapping_id)
        return {"deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/financial-forecasts/bands")
def api_financial_forecast_bands(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"bands": list_band_definitions(db)}


@router.get("/financial-forecasts")
def api_list_financial_forecasts(
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
    return list_financial_forecasts(db, fiscal_year=year)


@router.post("/financial-forecasts/fill-from-sources")
def api_fill_financial_forecasts_from_sources(
    body: FillFinancialForecastsBody,
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
        return fill_financial_forecasts_from_data_sources(
            db,
            fiscal_year=body.fiscal_year,
            farms=body.farms or None,
            fill_mode=body.fill_mode,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not refresh from data sources: {exc}",
        ) from exc


@router.put("/financial-forecasts")
def api_save_financial_forecasts(
    body: SaveFinancialForecastsBody,
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
        return save_financial_forecasts(
            db,
            fiscal_year=body.fiscal_year,
            band_id=body.band_id,
            rows=[row.model_dump() for row in body.rows],
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class HpScheduleBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    business: str = Field(default="CM", min_length=1, max_length=8)
    description: str = Field(default="", max_length=255)
    monthly_capital: float = Field(ge=0)
    monthly_interest: float = Field(ge=0)
    months: int = Field(ge=1, le=600)
    payment_day: int = Field(ge=1, le=31)
    start_month: dt.date


@router.get("/hp-schedules")
def api_list_hp_schedules(
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    return {"schedules": list_hp_schedules(db)}


@router.get("/hp-schedules/payment-chart")
def api_hp_payment_chart(
    business: str | None = Query(None),
    from_month: dt.date | None = Query(None),
    to_month: dt.date | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    try:
        return build_hp_payment_chart(
            db,
            business=business,
            from_month=from_month,
            to_month=to_month,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/hp-schedules")
def api_create_hp_schedule(
    body: HpScheduleBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        return {
            "schedule": create_hp_schedule(
                db,
                name=body.name,
                business=body.business,
                description=body.description,
                monthly_capital=body.monthly_capital,
                monthly_interest=body.monthly_interest,
                months=body.months,
                payment_day=body.payment_day,
                start_month=body.start_month,
                user_id=user.id,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/hp-schedules/{schedule_id}")
def api_update_hp_schedule(
    schedule_id: int,
    body: HpScheduleBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        return {
            "schedule": update_hp_schedule(
                db,
                schedule_id=schedule_id,
                name=body.name,
                business=body.business,
                description=body.description,
                monthly_capital=body.monthly_capital,
                monthly_interest=body.monthly_interest,
                months=body.months,
                payment_day=body.payment_day,
                start_month=body.start_month,
                user_id=user.id,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/hp-schedules/{schedule_id}")
def api_deactivate_hp_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        deactivate_hp_schedule(db, schedule_id=schedule_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class RentalAgreementBody(BaseModel):
    business: str = Field(default="CM", min_length=1, max_length=8)
    farm_name: str = Field(min_length=1, max_length=128)
    farm_size: float = Field(ge=0)


class RentalPaymentRowBody(BaseModel):
    agreement_id: int
    payment_month: dt.date
    amount: float | None = None


class SaveRentalPaymentsBody(BaseModel):
    fiscal_year: int
    rows: list[RentalPaymentRowBody] = Field(default_factory=list)


@router.get("/rental-agreements")
def api_rental_agreements_report(
    fiscal_year: int | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_BENCHMARKING)),
):
    fy = fiscal_year if fiscal_year is not None else available_fiscal_years()[0]
    try:
        return build_rental_agreements_report(db, fiscal_year=fy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rental-agreements")
def api_create_rental_agreement(
    body: RentalAgreementBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        return {
            "agreement": create_rental_agreement(
                db,
                business=body.business,
                farm_name=body.farm_name,
                farm_size=body.farm_size,
                user_id=user.id,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/rental-agreements/payments")
def api_save_rental_payments(
    body: SaveRentalPaymentsBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        return save_rental_payments(
            db,
            fiscal_year=body.fiscal_year,
            rows=[row.model_dump() for row in body.rows],
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/rental-agreements/{agreement_id}")
def api_update_rental_agreement(
    agreement_id: int,
    body: RentalAgreementBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        return {
            "agreement": update_rental_agreement(
                db,
                agreement_id=agreement_id,
                business=body.business,
                farm_name=body.farm_name,
                farm_size=body.farm_size,
                user_id=user.id,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/rental-agreements/{agreement_id}")
def api_deactivate_rental_agreement(
    agreement_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_BENCHMARKING_EDIT)),
):
    try:
        deactivate_rental_agreement(db, agreement_id=agreement_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
