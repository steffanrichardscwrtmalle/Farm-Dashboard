"""Tests for cropping forecasts."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.cropping_forecasts import (
    create_crop_type,
    cropping_cost_totals,
    deactivate_crop_type,
    future_budget_sources,
    get_cropping_forecast,
    line_costs,
    save_cropping_forecast,
    seed_crop_types_if_empty,
    update_crop_type,
)
from app.services.financial_data_sources import FINANCIAL_DATA_SOURCE_KEYS


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_budget_link_is_prepared_but_not_registered() -> None:
    keys = [source["key"] for source in future_budget_sources()]
    assert keys == [
        "cropping.chemical",
        "cropping.fertiliser",
        "cropping.seed",
        "cropping.harvest",
    ]
    assert set(keys).isdisjoint(FINANCIAL_DATA_SOURCE_KEYS)


def test_seed_crop_types_once(db: Session) -> None:
    seed_crop_types_if_empty(db)
    seed_crop_types_if_empty(db)
    forecast = get_cropping_forecast(db, fiscal_year=2027, farm="CM")
    assert [row["name"] for row in forecast["rows"]] == [
        "Maize",
        "Spring Barley",
        "Winter Wheat",
        "Hybrid Rye",
        "Grazing",
    ]
    perennial = {row["name"]: row["is_perennial"] for row in forecast["rows"]}
    assert perennial["Grazing"] is True
    assert perennial["Maize"] is False
    assert forecast["rows"][0]["cut_percentages"] == [100]
    assert forecast["rows"][0]["average_cuts"] == 1
    assert forecast["totals"]["variable_cost_total"] == 0
    assert forecast["totals"]["average_cuts"] is None


def test_annual_seed_uses_full_acreage_and_perennial_uses_reseed() -> None:
    annual = line_costs(
        is_perennial=False,
        acres=100,
        acres_to_reseed=10,
        chemical_cost_per_acre=20,
        fertiliser_cost_per_acre=40,
        seed_cost_per_acre=50,
        harvest_cost_per_acre=12,
        cut_percentages=[100],
        expected_dm_tonnes=500,
    )
    assert annual["chemical_total"] == 2000
    assert annual["fertiliser_total"] == 4000
    assert annual["seed_total"] == 5000
    assert annual["harvest_total"] == 1200
    assert annual["variable_cost_total"] == 12200
    assert annual["dm_per_acre"] == 5

    grazing = line_costs(
        is_perennial=True,
        acres=200,
        acres_to_reseed=25,
        chemical_cost_per_acre=10,
        fertiliser_cost_per_acre=30,
        seed_cost_per_acre=80,
        harvest_cost_per_acre=None,
        cut_percentages=[100],
        expected_dm_tonnes=None,
    )
    assert grazing["chemical_total"] == 2000
    assert grazing["fertiliser_total"] == 6000
    assert grazing["seed_total"] == 2000
    assert grazing["dm_per_acre"] is None


def test_save_is_per_farm_and_year(db: Session) -> None:
    seed_crop_types_if_empty(db)
    forecast = get_cropping_forecast(db, fiscal_year=2027, farm="cm")
    maize = next(row for row in forecast["rows"] if row["name"] == "Maize")
    grazing = next(row for row in forecast["rows"] if row["name"] == "Grazing")
    saved = save_cropping_forecast(
        db,
        fiscal_year=2027,
        farm="CM",
        rows=[
            {
                "crop_type_id": maize["crop_type_id"],
                "acres": 80,
                "cut_percentages": [100],
                "expected_dm_tonnes": 400,
                "chemical_cost_per_acre": 15,
                "fertiliser_cost_per_acre": 45,
                "seed_cost_per_acre": 60,
                "acres_to_reseed": None,
            },
            {
                "crop_type_id": grazing["crop_type_id"],
                "acres": 150,
                "cut_percentages": [100, 80, 50],
                "expected_dm_tonnes": 900,
                "chemical_cost_per_acre": 5,
                "fertiliser_cost_per_acre": 25,
                "seed_cost_per_acre": 70,
                "acres_to_reseed": 20,
            },
        ],
        user_id=1,
    )
    by_name = {row["name"]: row for row in saved["rows"]}
    assert by_name["Maize"]["seed_total"] == 4800
    assert by_name["Grazing"]["seed_total"] == 1400
    assert by_name["Grazing"]["chemical_total"] == 750
    assert by_name["Grazing"]["average_cuts"] == 2.3
    assert [cut["acres"] for cut in by_name["Grazing"]["cuts"]] == [150, 120, 75]
    assert saved["totals"]["expected_dm_tonnes"] == 1300
    assert saved["totals"]["average_cuts"] == 1.85

    other_farm = get_cropping_forecast(db, fiscal_year=2027, farm="GAD")
    assert other_farm["totals"]["variable_cost_total"] == 0
    other_year = get_cropping_forecast(db, fiscal_year=2028, farm="CM")
    assert other_year["totals"]["variable_cost_total"] == 0

    totals = cropping_cost_totals(db, fiscal_year=2027, farm="CM")
    assert totals["chemical"] == saved["totals"]["chemical_total"]
    assert totals["fertiliser"] == saved["totals"]["fertiliser_total"]
    assert totals["seed"] == saved["totals"]["seed_total"]
    assert totals["variable_cost"] == (
        totals["chemical"] + totals["fertiliser"] + totals["seed"] + totals["harvest"]
    )


def test_create_update_and_remove_crop(db: Session) -> None:
    created = create_crop_type(db, name="Wholecrop", is_perennial=False, user_id=1)
    updated = update_crop_type(
        db,
        crop_type_id=created["id"],
        name="Wholecrop Rye",
        is_perennial=True,
    )
    assert updated["is_perennial"] is True
    forecast = get_cropping_forecast(db, fiscal_year=2027, farm="GAD")
    assert [row["name"] for row in forecast["rows"]] == ["Wholecrop Rye"]

    with pytest.raises(ValueError, match="already exists"):
        create_crop_type(db, name="wholecrop rye", is_perennial=False, user_id=None)

    deactivate_crop_type(db, crop_type_id=created["id"])
    assert get_cropping_forecast(db, fiscal_year=2027, farm="GAD")["rows"] == []
    restored = create_crop_type(db, name="Wholecrop Rye", is_perennial=False, user_id=None)
    assert restored["id"] == created["id"]
    assert restored["is_perennial"] is False


def test_grass_cuts_are_a_share_of_the_acreage(db: Session) -> None:
    crop = create_crop_type(db, name="Grazing", is_perennial=True, user_id=None)
    saved = save_cropping_forecast(
        db,
        fiscal_year=2027,
        farm="CM",
        rows=[
            {
                "crop_type_id": crop["id"],
                "acres": 2000,
                "cut_percentages": [100, 80, 50, 0, 0],
                "harvest_cost_per_acre": 10,
            }
        ],
        user_id=None,
    )
    grazing = saved["rows"][0]
    assert grazing["average_cuts"] == 2.3
    assert [cut["label"] for cut in grazing["cuts"]] == [
        "1st",
        "2nd",
        "3rd",
        "4th",
        "5th",
    ]
    assert [cut["acres"] for cut in grazing["cuts"]] == [2000, 1600, 1000, 0, 0]
    assert grazing["harvested_acres"] == 4600
    assert grazing["harvest_total"] == 46000


def test_rejects_bad_farm_and_cuts(db: Session) -> None:
    crop = create_crop_type(db, name="Maize", is_perennial=False, user_id=None)
    with pytest.raises(ValueError, match="CM or GAD"):
        get_cropping_forecast(db, fiscal_year=2027, farm="BOTH")
    with pytest.raises(ValueError, match="more than 100%"):
        save_cropping_forecast(
            db,
            fiscal_year=2027,
            farm="CM",
            rows=[{"crop_type_id": crop["id"], "cut_percentages": [150]}],
            user_id=None,
        )
