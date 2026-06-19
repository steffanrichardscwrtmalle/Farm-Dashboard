from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_editor
from app.db import get_db
from app.models import BUSINESS_OPTIONS, SUPPLIER_WYNNSTAY, ProductMappingRule, User
from app.services.category_breakdown import get_category_breakdown
from app.services.product_price_by_month import get_product_price_by_month
from app.services.product_quantity_by_month import get_product_quantity_by_month
from app.services.monthly_spend import get_monthly_spend
from app.services.invoice_ops import (
    get_invoice_months,
    get_stats,
    get_unknown_products,
    import_excel_file,
    list_invoice_lines,
    refresh_all_invoice_lines,
)
from app.services.mappings import (
    import_rules_to_db,
    list_mapping_rules,
    load_rules_from_excel_bytes,
)
from app.services.mapping_options import (
    VALID_OPTION_TYPES,
    add_mapping_option,
    delete_mapping_option,
    list_mapping_option_rows,
    list_mapping_options,
    seed_mapping_options_if_empty,
    sync_options_from_values,
    validate_mapping_values,
)

router = APIRouter(prefix="/api")


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


@router.get("/stats")
def api_stats(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return get_stats(db)


@router.get("/invoice-months")
def api_invoice_months(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return {"items": get_invoice_months(db)}


@router.get("/category-breakdown")
def api_category_breakdown(
    from_month: str,
    to_month: str | None = None,
    include_credit: bool = True,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end_month = to_month or from_month
    try:
        return get_category_breakdown(
            db,
            from_month=from_month,
            to_month=end_month,
            include_credit=include_credit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/product-price-by-month")
def api_product_price_by_month(
    from_month: str,
    to_month: str | None = None,
    category: str | None = None,
    recent_only: bool = False,
    include_credit: bool = True,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end_month = to_month or from_month
    try:
        return get_product_price_by_month(
            db,
            from_month=from_month,
            to_month=end_month,
            category=category.strip() if category else None,
            recent_only=recent_only,
            include_credit=include_credit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/product-quantity-by-month")
def api_product_quantity_by_month(
    from_month: str,
    to_month: str | None = None,
    category: str | None = None,
    recent_only: bool = False,
    include_credit: bool = True,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end_month = to_month or from_month
    try:
        return get_product_quantity_by_month(
            db,
            from_month=from_month,
            to_month=end_month,
            category=category.strip() if category else None,
            recent_only=recent_only,
            include_credit=include_credit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/monthly-spend")
def api_monthly_spend(
    from_month: str,
    to_month: str | None = None,
    category: str | None = None,
    recent_only: bool = False,
    include_credit: bool = True,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end_month = to_month or from_month
    try:
        return get_monthly_spend(
            db,
            from_month=from_month,
            to_month=end_month,
            category=category.strip() if category else None,
            recent_only=recent_only,
            include_credit=include_credit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/invoice-lines")
def api_invoice_lines(
    recent: str | None = None,
    recent_only: bool = False,
    unknown: str | None = None,
    invoice_month: str | None = None,
    from_month: str | None = None,
    to_month: str | None = None,
    credit: str | None = None,
    limit: int = 500,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if unknown and unknown not in ("category", "farm", "any"):
        raise HTTPException(status_code=400, detail="unknown must be category, farm, or any")
    if credit and credit not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="credit must be yes or no")
    if (from_month and not to_month) or (to_month and not from_month):
        raise HTTPException(status_code=400, detail="from_month and to_month must be provided together")
    lines, total = list_invoice_lines(
        db,
        recent=recent,
        recent_only=recent_only,
        unknown=unknown,
        invoice_month=invoice_month,
        from_month=from_month,
        to_month=to_month,
        credit=credit,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [line.to_dict() for line in lines],
    }


@router.post("/imports")
async def api_import(
    file: UploadFile = File(...),
    invoice_date: str = Form(...),
    business: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_editor),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    business = business.strip()
    if business not in BUSINESS_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"business must be one of: {', '.join(BUSINESS_OPTIONS)}",
        )

    try:
        parsed_date = datetime.date.fromisoformat(invoice_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invoice_date must be YYYY-MM-DD") from exc

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        result = import_excel_file(db, content, file.filename, parsed_date, business)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result["rows_parsed"] == 0:
        raise HTTPException(status_code=400, detail="No data rows found in the uploaded file")

    return result


@router.post("/refresh")
def api_refresh(db: Session = Depends(get_db), _: User = Depends(require_editor)):
    count = refresh_all_invoice_lines(db)
    return {"rows_refreshed": count}


# --- Product mapping rules ---


@router.get("/mapping-options")
def api_list_mapping_options(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    seed_mapping_options_if_empty(db)
    return {
        "items": [row.to_dict() for row in list_mapping_option_rows(db)],
        **list_mapping_options(db),
    }


@router.post("/mapping-options")
def api_create_mapping_option(body: MappingOptionCreate, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    option_type = body.option_type.strip()
    if option_type not in VALID_OPTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"option_type must be one of: {', '.join(VALID_OPTION_TYPES)}",
        )
    try:
        option = add_mapping_option(db, option_type, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return option.to_dict()


@router.delete("/mapping-options/{option_id}")
def api_delete_mapping_option(option_id: int, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    try:
        delete_mapping_option(db, option_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": option_id}


@router.get("/mappings")
def api_list_mappings(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rules = list_mapping_rules(db)
    return {"items": [r.to_dict() for r in rules], "total": len(rules)}


@router.post("/mappings")
def api_create_mapping(body: MappingCreate, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Keyword is required")
    category = body.category.strip()
    farm_description = body.farm_description.strip()
    if not category:
        raise HTTPException(status_code=400, detail="Category is required")
    if not farm_description:
        raise HTTPException(status_code=400, detail="Farm description is required")
    try:
        validate_mapping_values(db, category, farm_description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    max_order = db.scalar(
        select(func.max(ProductMappingRule.sort_order)).where(
            ProductMappingRule.supplier == SUPPLIER_WYNNSTAY
        )
    ) or -1
    rule = ProductMappingRule(
        supplier=SUPPLIER_WYNNSTAY,
        keyword=keyword,
        category=category,
        farm_description=farm_description,
        sort_order=max_order + 1,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    refresh_all_invoice_lines(db)
    return rule.to_dict()


@router.put("/mappings/bulk")
def api_bulk_update_mappings(body: MappingBulkUpdateBody, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    updated = 0
    for item in body.items:
        rule = db.get(ProductMappingRule, item.id)
        if rule is None or rule.supplier != SUPPLIER_WYNNSTAY:
            raise HTTPException(status_code=404, detail=f"Rule not found: {item.id}")
        keyword = item.keyword.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="Keyword cannot be empty")
        category = item.category.strip()
        farm_description = item.farm_description.strip()
        if not category:
            raise HTTPException(status_code=400, detail="Category is required")
        if not farm_description:
            raise HTTPException(status_code=400, detail="Farm description is required")
        try:
            validate_mapping_values(db, category, farm_description)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rule.keyword = keyword
        rule.category = category
        rule.farm_description = farm_description
        updated += 1
    db.commit()
    refresh_all_invoice_lines(db)
    return {"updated": updated}


@router.put("/mappings/{rule_id}")
def api_update_mapping(rule_id: int, body: MappingUpdate, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    rule = db.get(ProductMappingRule, rule_id)
    if rule is None or rule.supplier != SUPPLIER_WYNNSTAY:
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
        validate_mapping_values(db, rule.category, rule.farm_description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    refresh_all_invoice_lines(db)
    return rule.to_dict()


@router.delete("/mappings/{rule_id}")
def api_delete_mapping(rule_id: int, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    rule = db.get(ProductMappingRule, rule_id)
    if rule is None or rule.supplier != SUPPLIER_WYNNSTAY:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    refresh_all_invoice_lines(db)
    return {"deleted": rule_id}


@router.post("/mappings/reorder")
def api_reorder_mappings(body: ReorderBody, db: Session = Depends(get_db), _: User = Depends(require_editor)):
    for item in body.items:
        rule = db.get(ProductMappingRule, item.id)
        if rule and rule.supplier == SUPPLIER_WYNNSTAY:
            rule.sort_order = item.sort_order
    db.commit()
    return {"updated": len(body.items)}


@router.post("/mappings/apply")
def api_apply_mappings(db: Session = Depends(get_db), _: User = Depends(require_editor)):
    count = refresh_all_invoice_lines(db)
    return {"rows_refreshed": count}


@router.post("/mappings/import-excel")
async def api_import_mappings_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_editor),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        rules = load_rules_from_excel_bytes(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rules:
        raise HTTPException(status_code=400, detail="No rules found in file")
    categories = {row.get("category", "").strip() for row in rules if row.get("category", "").strip()}
    farm_descriptions = {
        row.get("farm_description", "").strip()
        for row in rules
        if row.get("farm_description", "").strip()
    }
    sync_options_from_values(db, categories, farm_descriptions, supplier=SUPPLIER_WYNNSTAY)
    count = import_rules_to_db(db, rules, replace=True, supplier=SUPPLIER_WYNNSTAY)
    refresh_all_invoice_lines(db)
    return {"rules_imported": count}


@router.get("/mappings/unknown-products")
def api_unknown_products(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return {"items": get_unknown_products(db)}
