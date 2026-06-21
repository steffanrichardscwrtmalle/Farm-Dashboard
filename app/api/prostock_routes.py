"""Prostock API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_action, require_page
from app.auth.permissions import (
    ACTION_PROSTOCK_IMPORT,
    ACTION_PROSTOCK_MAPPINGS,
    PAGE_PROSTOCK,
)
from app.db import get_db
from app.models import PROSTOCK_BUSINESS_OPTIONS, SUPPLIER_PROSTOCK, ProductMappingRule, User
from app.services.mapping_options import (
    VALID_OPTION_TYPES,
    add_mapping_option,
    delete_mapping_option,
    list_mapping_option_rows,
    list_mapping_options,
    seed_mapping_options_if_empty,
    validate_mapping_values,
)
from app.services.mappings import list_mapping_rules
from app.services.prostock_mappings import (
    ensure_prostock_mappings_seeded,
    reseed_prostock_mappings_from_library,
)
from app.services.prostock_ops import (
    get_unknown_drugs,
    get_prostock_invoice_months,
    import_prostock_file,
    list_prostock_invoice_lines,
    refresh_prostock_lines,
)
from app.services.prostock_product_prices import (
    get_prostock_monthly_spend,
    get_prostock_product_prices,
    get_prostock_product_quantities,
)

router = APIRouter(prefix="/api/prostock")


class MappingCreate(BaseModel):
    keyword: str
    category: str = ""
    farm_description: str = ""


class MappingUpdate(BaseModel):
    keyword: str | None = None
    category: str | None = None
    farm_description: str | None = None


class MappingBulkUpdateItem(BaseModel):
    id: int
    keyword: str
    category: str
    farm_description: str


class MappingBulkUpdateBody(BaseModel):
    items: list[MappingBulkUpdateItem]


class ReorderItem(BaseModel):
    id: int
    sort_order: int


class ReorderBody(BaseModel):
    items: list[ReorderItem]


class MappingOptionCreate(BaseModel):
    option_type: str
    value: str


@router.post("/imports")
async def api_prostock_import(
    file: UploadFile = File(...),
    business: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_IMPORT)),
):
    if business not in PROSTOCK_BUSINESS_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"business must be one of: {', '.join(PROSTOCK_BUSINESS_OPTIONS)}",
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        result = import_prostock_file(db, content, file.filename or "upload.xlsx", business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/refresh")
def api_prostock_refresh(db: Session = Depends(get_db), _: User = Depends(require_action(ACTION_PROSTOCK_IMPORT))):
    count = refresh_prostock_lines(db)
    return {"rows_refreshed": count}


@router.get("/invoice-months")
def api_prostock_invoice_months(
    business: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_PROSTOCK)),
):
    return {"items": get_prostock_invoice_months(db, businesses=business or None)}


@router.get("/invoice-lines")
def api_prostock_invoice_lines(
    from_month: str | None = None,
    to_month: str | None = None,
    business: list[str] = Query(default=[]),
    limit: int = 500,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_PROSTOCK)),
):
    if (from_month and not to_month) or (to_month and not from_month):
        raise HTTPException(
            status_code=400, detail="from_month and to_month must be provided together"
        )
    lines, total = list_prostock_invoice_lines(
        db,
        businesses=business or None,
        from_month=from_month,
        to_month=to_month,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [line.to_dict() for line in lines],
    }


@router.get("/product-prices")
def api_prostock_product_prices(
    from_month: str,
    to_month: str,
    business: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_PROSTOCK)),
):
    try:
        return get_prostock_product_prices(
            db,
            from_month=from_month,
            to_month=to_month,
            businesses=business or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/product-quantities")
def api_prostock_product_quantities(
    from_month: str,
    to_month: str,
    business: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_PROSTOCK)),
):
    try:
        return get_prostock_product_quantities(
            db,
            from_month=from_month,
            to_month=to_month,
            businesses=business or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/monthly-spend")
def api_prostock_monthly_spend(
    from_month: str,
    to_month: str,
    business: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(require_page(PAGE_PROSTOCK)),
):
    try:
        return get_prostock_monthly_spend(
            db,
            from_month=from_month,
            to_month=to_month,
            businesses=business or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mapping-options")
def api_prostock_mapping_options(db: Session = Depends(get_db), _: User = Depends(require_page(PAGE_PROSTOCK))):
    ensure_prostock_mappings_seeded(db)
    return {
        "items": [row.to_dict() for row in list_mapping_option_rows(db, supplier=SUPPLIER_PROSTOCK)],
        **list_mapping_options(db, supplier=SUPPLIER_PROSTOCK),
    }


@router.post("/mapping-options")
def api_prostock_create_mapping_option(
    body: MappingOptionCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    option_type = body.option_type.strip()
    if option_type not in VALID_OPTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"option_type must be one of: {', '.join(VALID_OPTION_TYPES)}",
        )
    try:
        option = add_mapping_option(
            db, option_type, body.value, supplier=SUPPLIER_PROSTOCK
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return option.to_dict()


@router.delete("/mapping-options/{option_id}")
def api_prostock_delete_mapping_option(
    option_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    try:
        delete_mapping_option(db, option_id, supplier=SUPPLIER_PROSTOCK)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": option_id}


@router.get("/mappings")
def api_prostock_list_mappings(db: Session = Depends(get_db), _: User = Depends(require_page(PAGE_PROSTOCK))):
    ensure_prostock_mappings_seeded(db)
    rules = list_mapping_rules(db, supplier=SUPPLIER_PROSTOCK)
    return {"items": [r.to_dict() for r in rules], "total": len(rules)}


@router.post("/mappings")
def api_prostock_create_mapping(
    body: MappingCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Keyword is required")
    category = body.category.strip()
    farm_description = body.farm_description.strip()
    if not category:
        raise HTTPException(status_code=400, detail="Group is required")
    if not farm_description:
        raise HTTPException(status_code=400, detail="Product is required")
    try:
        validate_mapping_values(
            db, category, farm_description, supplier=SUPPLIER_PROSTOCK
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    max_order = db.scalar(
        select(func.max(ProductMappingRule.sort_order)).where(
            ProductMappingRule.supplier == SUPPLIER_PROSTOCK
        )
    ) or -1
    rule = ProductMappingRule(
        supplier=SUPPLIER_PROSTOCK,
        keyword=keyword,
        category=category,
        farm_description=farm_description,
        sort_order=max_order + 1,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    refresh_prostock_lines(db)
    return rule.to_dict()


@router.put("/mappings/bulk")
def api_prostock_bulk_update_mappings(
    body: MappingBulkUpdateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    updated = 0
    for item in body.items:
        rule = db.get(ProductMappingRule, item.id)
        if rule is None or rule.supplier != SUPPLIER_PROSTOCK:
            raise HTTPException(status_code=404, detail=f"Rule not found: {item.id}")
        keyword = item.keyword.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="Keyword cannot be empty")
        category = item.category.strip()
        farm_description = item.farm_description.strip()
        if not category or not farm_description:
            raise HTTPException(status_code=400, detail="Group and product are required")
        try:
            validate_mapping_values(
                db, category, farm_description, supplier=SUPPLIER_PROSTOCK
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rule.keyword = keyword
        rule.category = category
        rule.farm_description = farm_description
        updated += 1
    db.commit()
    refresh_prostock_lines(db)
    return {"updated": updated}


@router.put("/mappings/{rule_id}")
def api_prostock_update_mapping(
    rule_id: int,
    body: MappingUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    rule = db.get(ProductMappingRule, rule_id)
    if rule is None or rule.supplier != SUPPLIER_PROSTOCK:
        raise HTTPException(status_code=404, detail="Rule not found")
    if body.keyword is not None:
        kw = body.keyword.strip()
        if not kw:
            raise HTTPException(status_code=400, detail="Keyword cannot be empty")
        rule.keyword = kw
    if body.category is not None:
        rule.category = body.category.strip()
    if body.farm_description is not None:
        rule.farm_description = body.farm_description.strip()
    try:
        validate_mapping_values(
            db, rule.category, rule.farm_description, supplier=SUPPLIER_PROSTOCK
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    refresh_prostock_lines(db)
    return rule.to_dict()


@router.delete("/mappings/{rule_id}")
def api_prostock_delete_mapping(
    rule_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    rule = db.get(ProductMappingRule, rule_id)
    if rule is None or rule.supplier != SUPPLIER_PROSTOCK:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    refresh_prostock_lines(db)
    return {"deleted": rule_id}


@router.post("/mappings/reorder")
def api_prostock_reorder_mappings(
    body: ReorderBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS)),
):
    for item in body.items:
        rule = db.get(ProductMappingRule, item.id)
        if rule and rule.supplier == SUPPLIER_PROSTOCK:
            rule.sort_order = item.sort_order
    db.commit()
    return {"updated": len(body.items)}


@router.post("/mappings/apply")
def api_prostock_apply_mappings(db: Session = Depends(get_db), _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS))):
    count = refresh_prostock_lines(db)
    return {"rows_refreshed": count}


@router.get("/mappings/unknown-drugs")
def api_prostock_unknown_drugs(db: Session = Depends(get_db), _: User = Depends(require_page(PAGE_PROSTOCK))):
    return {"items": get_unknown_drugs(db)}


@router.post("/mappings/reseed-library")
def api_prostock_reseed_library(db: Session = Depends(get_db), _: User = Depends(require_action(ACTION_PROSTOCK_MAPPINGS))):
    count = reseed_prostock_mappings_from_library(db)
    seed_mapping_options_if_empty(db, supplier=SUPPLIER_PROSTOCK)
    refresh_prostock_lines(db)
    return {"seeded": count}
