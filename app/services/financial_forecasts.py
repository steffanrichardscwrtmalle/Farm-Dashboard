"""Financial forecast headings, category mappings and monthly budget amounts."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    FINANCIAL_OPTION_BAND,
    FINANCIAL_OPTION_GROUP,
    FINANCIAL_OPTION_HEADING,
    FINANCIAL_OPTION_ITEM_TYPE,
    FINANCIAL_OPTION_TYPES,
    HERD_FARM_OPTIONS,
    FinancialForecastLine,
    FinancialForecastMapping,
    FinancialForecastMappingSource,
    FinancialForecastOption,
)
from app.services.financial_data_sources import validate_data_source_keys
from app.services.benchmarking import (
    available_fiscal_years,
    fiscal_year_months,
    forecast_period_cutoff,
)

# (item_type, band, group, heading)
DEFAULT_FINANCIAL_MAPPINGS: tuple[tuple[str, str, str, str], ...] = (
    ("Cash", "Creditor Catchup", "Creditor Catchup", "Creditor Catchup"),
    ("Cash", "Current Liabilities", "HP", "Budget Capital Repayment HP"),
    ("Cash", "Current Liabilities", "HP", "New Cows"),
    ("Cash", "Current Liabilities", "HP", "Shed/ Machinery"),
    ("Cash", "Current Liabilities", "HP Received", "Wynnstay"),
    ("Cash", "Current Liabilities", "HP Received", "Shed/ Machinery"),
    ("Cash", "Current Liabilities", "Tax Bill", "Tax Bill"),
    ("Cash", "Long Term Liabilities", "Loans", "Budget Loan Capital Repayments"),
    ("Cash", "Long Term Liabilities", "Loans Received", "Loans Received"),
    ("Profit & Loss", "Capital Depreciation", "Capital Depreciation", "Capital Depreciation"),
    ("Profit & Loss", "Overhead Expenses", "Labour", "Labour"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Parlour Maintenance"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Machinery Reps"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Red Diesel"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Vehicle Repairs"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Veh' Tax & Ins"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Vehicle Fuel"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Oils & Greases"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Contracting"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Contracting Services"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Electricity"),
    ("Profit & Loss", "Overhead Expenses", "Power & Machinery", "Heating"),
    ("Profit & Loss", "Overhead Expenses", "Property", "Building Repairs"),
    ("Profit & Loss", "Overhead Expenses", "Property", "Fences & Yards"),
    ("Profit & Loss", "Overhead Expenses", "Property", "Farmhouse Reps"),
    ("Profit & Loss", "Overhead Expenses", "Property", "Water Charges"),
    ("Profit & Loss", "Overhead Expenses", "Property", "Council Tax"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "General Insurance"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "Telephone"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "Professional Fees"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "Auditor Fees"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "Office Admin"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "Subscriptions"),
    ("Profit & Loss", "Overhead Expenses", "Office & Admin", "HMRC Settlement"),
    ("Profit & Loss", "Overhead Expenses", "Rent", "Rent"),
    ("Profit & Loss", "Overhead Expenses", "Finance Costs", "Loan Account Interest"),
    ("Profit & Loss", "Overhead Expenses", "Finance Costs", "HP Interest"),
    ("Profit & Loss", "Overhead Expenses", "Finance Costs", "Current Account Charges"),
    ("Profit & Loss", "Overhead Expenses", "Finance Costs", "Current Account Interest"),
    ("Profit & Loss", "Overhead Expenses", "Directors Salaries", "Directors Salaries"),
    ("Profit & Loss", "Purchases", "Bought Stock", "Bought Stock"),
    ("Profit & Loss", "Purchases", "Bought Feed", "Concentrates"),
    ("Profit & Loss", "Purchases", "Bought Feed", "Straw Feed"),
    ("Profit & Loss", "Purchases", "Bought Feed", "Bulky Feed"),
    ("Profit & Loss", "Purchases", "Bought Feed", "Minerals"),
    ("Profit & Loss", "Purchases", "Vet & Med", "Vet & Med"),
    ("Profit & Loss", "Purchases", "Foot Trimming", "Foot Trim"),
    ("Profit & Loss", "Purchases", "AI Fees", "AI Fees"),
    ("Profit & Loss", "Purchases", "Bedding", "Bedding Sawdust"),
    ("Profit & Loss", "Purchases", "Bedding", "Bedding Straw"),
    ("Profit & Loss", "Purchases", "Bedding", "Bedding Sand"),
    ("Profit & Loss", "Purchases", "Dairy Chemicals", "Dairy Chemicals"),
    ("Profit & Loss", "Purchases", "Forage Chemicals", "Forage Chemicals"),
    ("Profit & Loss", "Purchases", "Seeds", "Seeds"),
    ("Profit & Loss", "Purchases", "Fertiliser", "Fertiliser"),
    ("Profit & Loss", "Purchases", "Auctioneers Fees", "Auctioneers Fees"),
    ("Profit & Loss", "Purchases", "Milk Deductions", "Milk Deductions"),
    ("Profit & Loss", "Purchases", "Fallen Stock", "Fallen Stock"),
    ("Profit & Loss", "Purchases", "Haulage", "Haulage"),
    ("Profit & Loss", "Purchases", "Sundry", "Sundry"),
    ("Profit & Loss", "Purchases", "Contract Rearing", "Contract Rearing"),
    ("Profit & Loss", "Sales", "Milk Sales", "Milk Sales"),
    ("Profit & Loss", "Sales", "Livestock Sales", "Breeding Stock"),
    ("Profit & Loss", "Sales", "Livestock Sales", "Beef Calves"),
    ("Profit & Loss", "Sales", "Livestock Sales", "Beef Sales"),
    ("Profit & Loss", "Sales", "Misc Revenue", "Misc Revenue"),
    ("Profit & Loss", "Sales", "Forage Sales", "Forage Sales"),
    ("Profit & Loss", "Sales", "Subsidies", "Basic Payment Scheme"),
    ("Profit & Loss", "Valuation Change", "Valuation Change", "Stock Valuation Change"),
    ("Profit & Loss", "Valuation Change", "Valuation Change", "Forage Valuation Change"),
)

ITEM_TYPE_ORDER: tuple[str, ...] = ("Cash", "Profit & Loss")


def _normalize(value: str) -> str:
    return value.strip()


def _option_exists(db: Session, option_type: str, value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    existing = db.scalars(
        select(FinancialForecastOption).where(
            FinancialForecastOption.option_type == option_type,
            func.lower(FinancialForecastOption.value) == normalized.lower(),
        )
    ).first()
    return existing is not None


def _ensure_option(db: Session, option_type: str, value: str, *, sort_order: int = 0) -> None:
    normalized = _normalize(value)
    if not normalized:
        return
    if _option_exists(db, option_type, normalized):
        return
    db.add(
        FinancialForecastOption(
            option_type=option_type,
            value=normalized,
            sort_order=sort_order,
        )
    )
    db.flush()


def seed_financial_forecasts_if_empty(db: Session) -> int:
    existing = db.scalars(select(FinancialForecastMapping).limit(1)).first()
    if existing is not None:
        return 0

    added = 0
    for idx, (item_type, band, group, heading) in enumerate(DEFAULT_FINANCIAL_MAPPINGS):
        _ensure_option(db, FINANCIAL_OPTION_ITEM_TYPE, item_type, sort_order=idx)
        _ensure_option(db, FINANCIAL_OPTION_BAND, band, sort_order=idx)
        _ensure_option(db, FINANCIAL_OPTION_GROUP, group, sort_order=idx)
        _ensure_option(db, FINANCIAL_OPTION_HEADING, heading, sort_order=idx)
        db.add(
            FinancialForecastMapping(
                heading=heading,
                item_type=item_type,
                band=band,
                group=group,
                sort_order=idx,
            )
        )
        added += 1
    db.commit()
    return added


def list_financial_options(db: Session) -> dict[str, Any]:
    options = db.scalars(
        select(FinancialForecastOption).order_by(
            FinancialForecastOption.option_type,
            FinancialForecastOption.sort_order,
            FinancialForecastOption.value,
        )
    ).all()
    payload: dict[str, list[str]] = {
        "item_types": [],
        "bands": [],
        "groups": [],
        "headings": [],
    }
    items: list[dict[str, Any]] = []
    for option in options:
        items.append(
            {
                "id": option.id,
                "option_type": option.option_type,
                "value": option.value,
                "sort_order": option.sort_order,
            }
        )
        if option.option_type == FINANCIAL_OPTION_ITEM_TYPE:
            payload["item_types"].append(option.value)
        elif option.option_type == FINANCIAL_OPTION_BAND:
            payload["bands"].append(option.value)
        elif option.option_type == FINANCIAL_OPTION_GROUP:
            payload["groups"].append(option.value)
        elif option.option_type == FINANCIAL_OPTION_HEADING:
            payload["headings"].append(option.value)
    payload["items"] = items
    return payload


def add_financial_option(db: Session, option_type: str, value: str) -> FinancialForecastOption:
    if option_type not in FINANCIAL_OPTION_TYPES:
        raise ValueError(f"option_type must be one of: {', '.join(FINANCIAL_OPTION_TYPES)}")
    normalized = _normalize(value)
    if not normalized:
        raise ValueError("Value is required")
    existing = db.scalars(
        select(FinancialForecastOption).where(
            FinancialForecastOption.option_type == option_type,
            func.lower(FinancialForecastOption.value) == normalized.lower(),
        )
    ).first()
    if existing:
        return existing
    option = FinancialForecastOption(option_type=option_type, value=normalized)
    db.add(option)
    db.commit()
    db.refresh(option)
    return option


def delete_financial_option(db: Session, option_id: int) -> None:
    option = db.get(FinancialForecastOption, option_id)
    if option is None:
        raise ValueError("Option not found")

    if option.option_type == FINANCIAL_OPTION_HEADING:
        in_use = db.scalar(
            select(func.count())
            .select_from(FinancialForecastMapping)
            .where(FinancialForecastMapping.heading == option.value)
        ) or 0
    elif option.option_type == FINANCIAL_OPTION_ITEM_TYPE:
        in_use = db.scalar(
            select(func.count())
            .select_from(FinancialForecastMapping)
            .where(FinancialForecastMapping.item_type == option.value)
        ) or 0
    elif option.option_type == FINANCIAL_OPTION_BAND:
        in_use = db.scalar(
            select(func.count())
            .select_from(FinancialForecastMapping)
            .where(FinancialForecastMapping.band == option.value)
        ) or 0
    else:
        in_use = db.scalar(
            select(func.count())
            .select_from(FinancialForecastMapping)
            .where(FinancialForecastMapping.group == option.value)
        ) or 0

    if in_use:
        raise ValueError("Cannot delete — this value is still used by heading mappings or forecasts")

    db.delete(option)
    db.commit()


def _validate_mapping_values(
    db: Session,
    *,
    item_type: str,
    band: str,
    group: str,
    heading: str,
) -> None:
    for option_type, value in (
        (FINANCIAL_OPTION_ITEM_TYPE, item_type),
        (FINANCIAL_OPTION_BAND, band),
        (FINANCIAL_OPTION_GROUP, group),
        (FINANCIAL_OPTION_HEADING, heading),
    ):
        if not _option_exists(db, option_type, value):
            raise ValueError(f"'{value}' is not in the allowed {option_type.replace('_', ' ')} list")


def _mapping_sources_by_id(db: Session, mapping_ids: list[int]) -> dict[int, list[str]]:
    if not mapping_ids:
        return {}
    rows = db.scalars(
        select(FinancialForecastMappingSource)
        .where(FinancialForecastMappingSource.mapping_id.in_(mapping_ids))
        .order_by(FinancialForecastMappingSource.source_key)
    ).all()
    payload: dict[int, list[str]] = {mapping_id: [] for mapping_id in mapping_ids}
    for row in rows:
        payload.setdefault(row.mapping_id, []).append(row.source_key)
    return payload


def _set_mapping_sources(
    db: Session,
    mapping_id: int,
    source_keys: list[str] | None,
) -> list[str]:
    normalized = validate_data_source_keys(source_keys or [])
    existing = db.scalars(
        select(FinancialForecastMappingSource).where(
            FinancialForecastMappingSource.mapping_id == mapping_id
        )
    ).all()
    for row in existing:
        db.delete(row)
    for key in normalized:
        db.add(FinancialForecastMappingSource(mapping_id=mapping_id, source_key=key))
    return normalized


def list_financial_mappings(db: Session) -> list[dict[str, Any]]:
    mappings = db.scalars(
        select(FinancialForecastMapping).order_by(
            FinancialForecastMapping.item_type,
            FinancialForecastMapping.band,
            FinancialForecastMapping.group,
            FinancialForecastMapping.sort_order,
            FinancialForecastMapping.heading,
        )
    ).all()
    sources_by_id = _mapping_sources_by_id(db, [row.id for row in mappings])
    return [
        {
            "id": row.id,
            "heading": row.heading,
            "item_type": row.item_type,
            "band": row.band,
            "group": row.group,
            "sort_order": row.sort_order,
            "data_sources": sources_by_id.get(row.id, []),
        }
        for row in mappings
    ]


def create_financial_mapping(
    db: Session,
    *,
    heading: str,
    item_type: str,
    band: str,
    group: str,
    data_sources: list[str] | None = None,
) -> FinancialForecastMapping:
    heading = _normalize(heading)
    item_type = _normalize(item_type)
    band = _normalize(band)
    group = _normalize(group)
    if not all([heading, item_type, band, group]):
        raise ValueError("Heading, item type, band and group are all required")

    _validate_mapping_values(db, item_type=item_type, band=band, group=group, heading=heading)

    existing = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.item_type == item_type,
            FinancialForecastMapping.band == band,
            FinancialForecastMapping.group == group,
            func.lower(FinancialForecastMapping.heading) == heading.lower(),
        )
    ).first()
    if existing is not None:
        raise ValueError("This heading mapping already exists")

    mapping = FinancialForecastMapping(
        heading=heading,
        item_type=item_type,
        band=band,
        group=group,
    )
    db.add(mapping)
    db.flush()
    _set_mapping_sources(db, mapping.id, data_sources)
    db.commit()
    db.refresh(mapping)
    return mapping


def update_financial_mapping(
    db: Session,
    mapping_id: int,
    *,
    heading: str,
    item_type: str,
    band: str,
    group: str,
    data_sources: list[str] | None = None,
) -> FinancialForecastMapping:
    mapping = db.get(FinancialForecastMapping, mapping_id)
    if mapping is None:
        raise ValueError("Mapping not found")

    heading = _normalize(heading)
    item_type = _normalize(item_type)
    band = _normalize(band)
    group = _normalize(group)
    if not all([heading, item_type, band, group]):
        raise ValueError("Heading, item type, band and group are all required")

    _validate_mapping_values(db, item_type=item_type, band=band, group=group, heading=heading)

    duplicate = db.scalars(
        select(FinancialForecastMapping).where(
            FinancialForecastMapping.item_type == item_type,
            FinancialForecastMapping.band == band,
            FinancialForecastMapping.group == group,
            func.lower(FinancialForecastMapping.heading) == heading.lower(),
            FinancialForecastMapping.id != mapping_id,
        )
    ).first()
    if duplicate is not None:
        raise ValueError("This heading mapping already exists")

    mapping.heading = heading
    mapping.item_type = item_type
    mapping.band = band
    mapping.group = group

    if data_sources is not None:
        _set_mapping_sources(db, mapping.id, data_sources)

    db.commit()
    db.refresh(mapping)
    return mapping


def delete_financial_mapping(db: Session, mapping_id: int) -> None:
    mapping = db.get(FinancialForecastMapping, mapping_id)
    if mapping is None:
        raise ValueError("Mapping not found")

    for line in db.scalars(
        select(FinancialForecastLine).where(FinancialForecastLine.mapping_id == mapping.id)
    ).all():
        db.delete(line)

    db.delete(mapping)
    db.commit()


def list_band_definitions(db: Session) -> list[dict[str, Any]]:
    mappings = list_financial_mappings(db)
    bands: dict[str, dict[str, Any]] = {}
    for row in mappings:
        key = f"{row['item_type']}|{row['band']}"
        if key not in bands:
            bands[key] = {
                "id": key,
                "item_type": row["item_type"],
                "band": row["band"],
                "label": f"{row['band']} ({row['item_type']})",
                "headings": [],
            }
        bands[key]["headings"].append(
            {
                "mapping_id": row["id"],
                "heading": row["heading"],
                "group": row["group"],
                "label": row["heading"],
                "data_sources": row["data_sources"],
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        item_type_rank = ITEM_TYPE_ORDER.index(item["item_type"]) if item["item_type"] in ITEM_TYPE_ORDER else 99
        return item_type_rank, item["band"]

    return sorted(bands.values(), key=sort_key)


def _empty_farm_amounts() -> dict[str, float | None]:
    return {farm: None for farm in HERD_FARM_OPTIONS}


def list_financial_forecasts(db: Session, *, fiscal_year: int) -> dict[str, Any]:
    months = fiscal_year_months(fiscal_year)
    month_set = set(months)
    bands = list_band_definitions(db)

    stored = db.scalars(
        select(FinancialForecastLine).where(FinancialForecastLine.fiscal_year == fiscal_year)
    ).all()

    by_mapping_month: dict[int, dict[dt.date, dict[str, float | None]]] = {}
    for line in stored:
        if line.forecast_month not in month_set:
            continue
        if line.farm not in HERD_FARM_OPTIONS:
            continue
        month_bucket = by_mapping_month.setdefault(line.mapping_id, {})
        farm_bucket = month_bucket.setdefault(line.forecast_month, _empty_farm_amounts())
        farm_bucket[line.farm] = line.amount

    bands_payload: dict[str, Any] = {}
    grid_rows: list[dict[str, Any]] = []
    for band_def in bands:
        headings_payload: dict[str, Any] = {}
        for heading_info in band_def["headings"]:
            mapping_id = heading_info["mapping_id"]
            heading_key = str(mapping_id)
            is_auto = bool(heading_info.get("data_sources"))
            month_rows: list[dict[str, Any]] = []
            for month_start in months:
                farm_cells = by_mapping_month.get(mapping_id, {}).get(
                    month_start, _empty_farm_amounts()
                )
                month_rows.append(
                    {
                        "forecast_month": month_start.isoformat(),
                        "month_label": month_start.strftime("%b-%y"),
                        "CM": farm_cells.get("CM"),
                        "GAD": farm_cells.get("GAD"),
                    }
                )
            heading_payload = {
                "mapping_id": mapping_id,
                "heading": heading_info["heading"],
                "group": heading_info["group"],
                "label": heading_info["label"],
                "auto": is_auto,
                "data_sources": heading_info.get("data_sources", []),
                "months": month_rows,
                "rows": month_rows,
            }
            headings_payload[heading_key] = heading_payload
            grid_rows.append(
                {
                    "mapping_id": mapping_id,
                    "item_type": band_def["item_type"],
                    "band": band_def["band"],
                    "group": heading_info["group"],
                    "heading": heading_info["heading"],
                    "auto": is_auto,
                    "months": month_rows,
                }
            )
        bands_payload[band_def["id"]] = {
            "item_type": band_def["item_type"],
            "band": band_def["band"],
            "label": band_def["label"],
            "headings": headings_payload,
        }

    month_labels = [
        {"forecast_month": m.isoformat(), "month_label": m.strftime("%b-%y")}
        for m in months
    ]

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_options": available_fiscal_years(),
        "months": [m.isoformat() for m in months],
        "month_labels": month_labels,
        "bands": bands_payload,
        "band_order": [b["id"] for b in bands],
        "grid_rows": grid_rows,
        **forecast_period_cutoff(),
    }


def _parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upsert_financial_forecast_rows(
    db: Session,
    *,
    fiscal_year: int,
    rows: list[dict[str, Any]],
    allowed_mapping_ids: set[int] | None,
    user_id: int | None,
) -> dict[str, int]:
    months = set(fiscal_year_months(fiscal_year))
    saved = 0
    deleted = 0

    for row in rows:
        mapping_id = row.get("mapping_id")
        if isinstance(mapping_id, str):
            mapping_id = int(mapping_id)
        if allowed_mapping_ids is not None and mapping_id not in allowed_mapping_ids:
            raise ValueError(f"Mapping {mapping_id} is not allowed for this save")

        forecast_month = row.get("forecast_month")
        if isinstance(forecast_month, str):
            forecast_month = dt.date.fromisoformat(forecast_month)
        if forecast_month not in months:
            continue

        for farm in HERD_FARM_OPTIONS:
            amount = _parse_optional_float(row.get(farm))
            existing = db.scalar(
                select(FinancialForecastLine).where(
                    FinancialForecastLine.fiscal_year == fiscal_year,
                    FinancialForecastLine.forecast_month == forecast_month,
                    FinancialForecastLine.mapping_id == mapping_id,
                    FinancialForecastLine.farm == farm,
                )
            )
            if amount is None:
                if existing is not None:
                    db.delete(existing)
                    deleted += 1
                continue
            if existing is None:
                db.add(
                    FinancialForecastLine(
                        fiscal_year=fiscal_year,
                        forecast_month=forecast_month,
                        mapping_id=mapping_id,
                        farm=farm,
                        amount=amount,
                        updated_by_user_id=user_id,
                    )
                )
                saved += 1
            else:
                existing.amount = amount
                existing.updated_by_user_id = user_id
                saved += 1

    db.commit()
    return {"saved": saved, "deleted": deleted}


def save_financial_forecasts(
    db: Session,
    *,
    fiscal_year: int,
    band_id: str,
    rows: list[dict[str, Any]],
    user_id: int | None,
) -> dict[str, int]:
    if band_id == "all":
        valid_ids = {
        row.id for row in db.scalars(select(FinancialForecastMapping)).all()
    }
        return _upsert_financial_forecast_rows(
            db,
            fiscal_year=fiscal_year,
            rows=rows,
            allowed_mapping_ids=valid_ids,
            user_id=user_id,
        )

    band_defs = {b["id"]: b for b in list_band_definitions(db)}
    if band_id not in band_defs:
        raise ValueError(f"Unknown band: {band_id}")

    allowed_mapping_ids = {h["mapping_id"] for h in band_defs[band_id]["headings"]}
    return _upsert_financial_forecast_rows(
        db,
        fiscal_year=fiscal_year,
        rows=rows,
        allowed_mapping_ids=allowed_mapping_ids,
        user_id=user_id,
    )
