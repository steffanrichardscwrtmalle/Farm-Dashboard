"""Annual cropping plans for the Budgets section.

Figures are stored per farm and financial year so they can later fill the
chemical, fertiliser and seed lines in Financial Forecasts. That link is not
wired yet: call ``cropping_cost_totals`` when it is. Expected dry matter stays
on this plan for a future forage link and is not a £ budget figure.

Cost rules used by the totals:
- Chemical and fertiliser apply to the full acreage.
- Seed applies to the full acreage for an annual crop.
- Seed applies only to acres to reseed for a perennial crop.
- Harvest cost is £ per acre, charged on the acres taken at each cut.
Each cut stores the percentage of the crop acreage taken at that cut.
Cuts is the sum of those percentages (100% + 80% + 50% is 2.3).
A crop with no cuts saved is treated as one cut of the whole acreage.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import HERD_FARM_OPTIONS, CropType, CroppingForecastLine
from app.services.farm_schedule import FARM_LABELS

# (name, is_perennial). Grazing is the only perennial in the starter list;
# any crop can be marked perennial later, which reveals acres to reseed.
DEFAULT_CROP_TYPES: tuple[tuple[str, bool], ...] = (
    ("Maize", False),
    ("Spring Barley", False),
    ("Winter Wheat", False),
    ("Hybrid Rye", False),
    ("Grazing", True),
)

# Reserved for the later Financial Forecasts data-source registry.
# Do not register these until cropping is meant to write budget lines.
FUTURE_BUDGET_SOURCE_KEYS: tuple[tuple[str, str], ...] = (
    ("cropping.chemical", "Cropping — chemical (£)"),
    ("cropping.fertiliser", "Cropping — fertiliser (£)"),
    ("cropping.seed", "Cropping — seed (£)"),
    ("cropping.harvest", "Cropping — harvest (£)"),
)


def future_budget_sources() -> list[dict[str, str]]:
    """Keys to add to the financial data-source registry when the budget link is switched on."""
    return [{"key": key, "label": label} for key, label in FUTURE_BUDGET_SOURCE_KEYS]


def seed_crop_types_if_empty(db: Session) -> None:
    existing = db.scalar(select(func.count()).select_from(CropType))
    if existing:
        return
    for index, (name, is_perennial) in enumerate(DEFAULT_CROP_TYPES):
        db.add(
            CropType(
                name=name,
                is_perennial=is_perennial,
                sort_order=index,
            )
        )
    db.commit()


def _farm(farm: str) -> str:
    code = (farm or "").strip().upper()
    if code not in HERD_FARM_OPTIONS:
        raise ValueError("Farm must be CM or GAD")
    return code


def _crop_payload(row: CropType) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "is_perennial": bool(row.is_perennial),
        "sort_order": row.sort_order,
    }


def list_crop_types(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CropType)
        .where(CropType.is_active.is_(True))
        .order_by(CropType.sort_order, CropType.name)
    ).all()
    return [_crop_payload(row) for row in rows]


def _find_by_name(db: Session, name: str) -> CropType | None:
    return db.scalar(select(CropType).where(func.lower(CropType.name) == name.lower()))


def create_crop_type(
    db: Session,
    *,
    name: str,
    is_perennial: bool,
    user_id: int | None,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Crop name is required")
    existing = _find_by_name(db, clean_name)
    if existing is not None and existing.is_active:
        raise ValueError(f"A crop named '{clean_name}' already exists")
    if existing is not None:
        existing.name = clean_name
        existing.is_perennial = bool(is_perennial)
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return _crop_payload(existing)

    max_order = db.scalar(select(func.coalesce(func.max(CropType.sort_order), -1)))
    row = CropType(
        name=clean_name,
        is_perennial=bool(is_perennial),
        sort_order=int(max_order or -1) + 1,
        created_by_user_id=user_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A crop named '{clean_name}' already exists") from exc
    db.refresh(row)
    return _crop_payload(row)


def update_crop_type(
    db: Session,
    *,
    crop_type_id: int,
    name: str,
    is_perennial: bool,
) -> dict[str, Any]:
    row = db.get(CropType, crop_type_id)
    if row is None or not row.is_active:
        raise ValueError("Crop not found")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Crop name is required")
    other = _find_by_name(db, clean_name)
    if other is not None and other.id != row.id:
        raise ValueError(f"A crop named '{clean_name}' already exists")
    row.name = clean_name
    row.is_perennial = bool(is_perennial)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"A crop named '{clean_name}' already exists") from exc
    db.refresh(row)
    return _crop_payload(row)


def deactivate_crop_type(db: Session, *, crop_type_id: int) -> None:
    row = db.get(CropType, crop_type_id)
    if row is None or not row.is_active:
        raise ValueError("Crop not found")
    row.is_active = False
    db.commit()


def _optional_number(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if number < 0:
        raise ValueError(f"{label} cannot be negative")
    return number


MAX_CUTS = 8
DEFAULT_CUT_PERCENTAGES: tuple[float, ...] = (100.0,)


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def normalize_cut_percentages(value: Any) -> list[float]:
    """Return cut percentages. Missing or empty means one cut of the whole acreage."""
    if value is None or value == "":
        return list(DEFAULT_CUT_PERCENTAGES)
    if not isinstance(value, (list, tuple)):
        raise ValueError("Cuts must be a list of percentages")
    if len(value) == 0:
        return list(DEFAULT_CUT_PERCENTAGES)
    if len(value) > MAX_CUTS:
        raise ValueError(f"A crop can have at most {MAX_CUTS} cuts")
    cleaned: list[float] = []
    for index, raw in enumerate(value, start=1):
        number = _optional_number(raw, f"Cut {index}")
        if number is None:
            number = 0.0
        if number > 100:
            raise ValueError(f"Cut {index} cannot be more than 100%")
        cleaned.append(round(number, 2))
    return cleaned


def average_cuts(percentages: list[float]) -> float:
    """Sum of cut shares. 100% + 80% + 50% is an average of 2.3 cuts."""
    return round(sum(percentages) / 100.0, 2)


def cut_breakdown(
    percentages: list[float],
    acres: float | None,
) -> list[dict[str, Any]]:
    """Acres taken at each cut: percentage of the crop acreage."""
    return [
        {
            "cut": index,
            "label": _ordinal(index),
            "percent": percent,
            "acres": (
                round(acres * percent / 100.0, 2) if acres is not None else None
            ),
        }
        for index, percent in enumerate(percentages, start=1)
    ]


def harvested_acres(acres: float | None, percentages: list[float]) -> float:
    """Acres taken across every cut. One full cut of 100 acres is 100."""
    return round((acres or 0.0) * sum(percentages) / 100.0, 2)


def line_costs(
    *,
    is_perennial: bool,
    acres: float | None,
    acres_to_reseed: float | None,
    chemical_cost_per_acre: float | None,
    fertiliser_cost_per_acre: float | None,
    seed_cost_per_acre: float | None,
    harvest_cost_per_acre: float | None,
    cut_percentages: list[float] | None,
    expected_dm_tonnes: float | None,
) -> dict[str, float | None]:
    """£ totals and dry-matter yield for one crop line."""
    area = acres or 0.0
    reseed = acres_to_reseed or 0.0
    seed_acres = reseed if is_perennial else area
    percentages = normalize_cut_percentages(cut_percentages)
    cut_acres = harvested_acres(acres, percentages)
    chemical_total = round(area * (chemical_cost_per_acre or 0.0), 2)
    fertiliser_total = round(area * (fertiliser_cost_per_acre or 0.0), 2)
    seed_total = round(seed_acres * (seed_cost_per_acre or 0.0), 2)
    harvest_total = round(cut_acres * (harvest_cost_per_acre or 0.0), 2)
    dm_per_acre = (
        round(expected_dm_tonnes / area, 3)
        if area and expected_dm_tonnes is not None
        else None
    )
    return {
        "seed_acres": seed_acres,
        "harvested_acres": cut_acres,
        "chemical_total": chemical_total,
        "fertiliser_total": fertiliser_total,
        "seed_total": seed_total,
        "harvest_total": harvest_total,
        "variable_cost_total": round(
            chemical_total + fertiliser_total + seed_total + harvest_total, 2
        ),
        "dm_per_acre": dm_per_acre,
    }


def _line_payload(crop: dict[str, Any], line: CroppingForecastLine | None) -> dict[str, Any]:
    acres = line.acres if line else None
    percentages = normalize_cut_percentages(
        line.cut_percentages if line is not None else None
    )
    expected_dm = line.expected_dm_tonnes if line else None
    chemical = line.chemical_cost_per_acre if line else None
    fertiliser = line.fertiliser_cost_per_acre if line else None
    seed = line.seed_cost_per_acre if line else None
    harvest = line.harvest_cost_per_acre if line else None
    reseed = line.acres_to_reseed if line else None
    costs = line_costs(
        is_perennial=crop["is_perennial"],
        acres=acres,
        acres_to_reseed=reseed,
        chemical_cost_per_acre=chemical,
        fertiliser_cost_per_acre=fertiliser,
        seed_cost_per_acre=seed,
        harvest_cost_per_acre=harvest,
        cut_percentages=percentages,
        expected_dm_tonnes=expected_dm,
    )
    return {
        "crop_type_id": crop["id"],
        "name": crop["name"],
        "is_perennial": crop["is_perennial"],
        "sort_order": crop["sort_order"],
        "acres": acres,
        "cut_percentages": percentages,
        "average_cuts": average_cuts(percentages),
        "cuts": cut_breakdown(percentages, acres),
        "expected_dm_tonnes": expected_dm,
        "chemical_cost_per_acre": chemical,
        "fertiliser_cost_per_acre": fertiliser,
        "seed_cost_per_acre": seed,
        "harvest_cost_per_acre": harvest,
        "acres_to_reseed": reseed,
        **costs,
    }


def _empty_totals() -> dict[str, float]:
    return {
        "acres": 0.0,
        "expected_dm_tonnes": 0.0,
        "chemical_total": 0.0,
        "fertiliser_total": 0.0,
        "seed_total": 0.0,
        "harvest_total": 0.0,
        "variable_cost_total": 0.0,
    }


def _add_totals(totals: dict[str, float], row: dict[str, Any]) -> None:
    totals["acres"] = round(totals["acres"] + (row["acres"] or 0.0), 2)
    totals["expected_dm_tonnes"] = round(
        totals["expected_dm_tonnes"] + (row["expected_dm_tonnes"] or 0.0), 3
    )
    totals["chemical_total"] = round(
        totals["chemical_total"] + float(row["chemical_total"]), 2
    )
    totals["fertiliser_total"] = round(
        totals["fertiliser_total"] + float(row["fertiliser_total"]), 2
    )
    totals["seed_total"] = round(totals["seed_total"] + float(row["seed_total"]), 2)
    totals["harvest_total"] = round(
        totals["harvest_total"] + float(row["harvest_total"]), 2
    )
    totals["variable_cost_total"] = round(
        totals["variable_cost_total"] + float(row["variable_cost_total"]), 2
    )


def get_cropping_forecast(
    db: Session,
    *,
    fiscal_year: int,
    farm: str,
) -> dict[str, Any]:
    farm_code = _farm(farm)
    crops = list_crop_types(db)
    lines = db.scalars(
        select(CroppingForecastLine).where(
            CroppingForecastLine.fiscal_year == fiscal_year,
            CroppingForecastLine.farm == farm_code,
        )
    ).all()
    by_crop = {line.crop_type_id: line for line in lines}
    rows = [_line_payload(crop, by_crop.get(crop["id"])) for crop in crops]
    totals = _empty_totals()
    weighted_cuts = 0.0
    weighted_acres = 0.0
    for row in rows:
        _add_totals(totals, row)
        if row["acres"]:
            weighted_cuts += row["acres"] * row["average_cuts"]
            weighted_acres += row["acres"]
    totals["average_cuts"] = (
        round(weighted_cuts / weighted_acres, 2) if weighted_acres else None
    )
    return {
        "fiscal_year": int(fiscal_year),
        "farm": farm_code,
        "farm_label": FARM_LABELS[farm_code],
        "rows": rows,
        "totals": totals,
    }


def cropping_cost_totals(
    db: Session,
    *,
    fiscal_year: int,
    farm: str,
) -> dict[str, float]:
    """Annual £ totals for a later budget feed. Not written to forecasts yet."""
    forecast = get_cropping_forecast(db, fiscal_year=fiscal_year, farm=farm)
    totals = forecast["totals"]
    return {
        "chemical": totals["chemical_total"],
        "fertiliser": totals["fertiliser_total"],
        "seed": totals["seed_total"],
        "harvest": totals["harvest_total"],
        "variable_cost": totals["variable_cost_total"],
        "expected_dm_tonnes": totals["expected_dm_tonnes"],
    }


def save_cropping_forecast(
    db: Session,
    *,
    fiscal_year: int,
    farm: str,
    rows: list[dict[str, Any]],
    user_id: int | None,
) -> dict[str, Any]:
    farm_code = _farm(farm)
    crops = {
        crop.id: crop
        for crop in db.scalars(select(CropType).where(CropType.is_active.is_(True)))
    }
    seen: set[int] = set()
    for raw in rows:
        crop_id = int(raw["crop_type_id"])
        if crop_id in seen:
            raise ValueError("Each crop can only appear once")
        seen.add(crop_id)
        crop = crops.get(crop_id)
        if crop is None:
            raise ValueError("Unknown crop")
        values = {
            "acres": _optional_number(raw.get("acres"), "Acres"),
            "cut_percentages": normalize_cut_percentages(raw.get("cut_percentages")),
            "expected_dm_tonnes": _optional_number(
                raw.get("expected_dm_tonnes"), "Expected dry matter"
            ),
            "chemical_cost_per_acre": _optional_number(
                raw.get("chemical_cost_per_acre"), "Chemical cost per acre"
            ),
            "fertiliser_cost_per_acre": _optional_number(
                raw.get("fertiliser_cost_per_acre"), "Fertiliser cost per acre"
            ),
            "seed_cost_per_acre": _optional_number(
                raw.get("seed_cost_per_acre"), "Seed cost per acre"
            ),
            "harvest_cost_per_acre": _optional_number(
                raw.get("harvest_cost_per_acre"), "Harvest cost"
            ),
            "acres_to_reseed": _optional_number(
                raw.get("acres_to_reseed"), "Acres to reseed"
            ),
        }
        line = db.scalar(
            select(CroppingForecastLine).where(
                CroppingForecastLine.fiscal_year == fiscal_year,
                CroppingForecastLine.farm == farm_code,
                CroppingForecastLine.crop_type_id == crop_id,
            )
        )
        if line is None:
            line = CroppingForecastLine(
                fiscal_year=fiscal_year,
                farm=farm_code,
                crop_type_id=crop_id,
            )
            db.add(line)
        for field, value in values.items():
            setattr(line, field, value)
        line.updated_by_user_id = user_id
    db.commit()
    return get_cropping_forecast(db, fiscal_year=fiscal_year, farm=farm_code)
