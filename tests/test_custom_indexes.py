from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.services.custom_indexes import (
    DEFAULT_INDEX_SETTINGS,
    attach_custom_indexes,
    dp_index,
    fw_index,
    load_index_settings,
    merge_index_settings,
    reset_index_settings,
    save_index_settings,
)

# Genosource Jumpstart P (August 2026 genomic list).
_JUMPSTART = {
    "milk_kg": 775,
    "fat_pct": 0.28,
    "protein_pct": 0.12,
    "fertility_index": 5.5,
    "lifespan_days": 101,
    "scc": -11,
    "mastitis": -2,
    "lameness": 2.4,
}


def test_dp_index_matches_spreadsheet_formula() -> None:
    milk_pta, fatpct, protpct = 775, 0.28, 0.12
    milk = milk_pta * 6.2 * 2 / 100
    fat = (((fatpct + 4.29) * milk_pta * 2.9) + (9000 * fatpct * 2.9)) / 100 * 2
    protein = (((protpct + 3.36) * milk_pta * 6.6) + (9000 * protpct * 6.6)) / 100 * 2
    expected = milk + fat + protein + 5.5 * 6 * 2 + 101 * 0.2 * 2 + (-11) * -2.2514 + 2.4 * 2.5 * 2
    assert round(dp_index(_JUMPSTART), 4) == round(expected, 4)


def test_fw_index_matches_spreadsheet_formula() -> None:
    milk_pta, fatpct, protpct = 775, 0.28, 0.12
    milk = milk_pta * 40 * 2 / 100
    fat = (((fatpct + 4) * milk_pta * 2.5) + (13000 * fatpct * 2.5)) / 100 * 2
    protein = (((protpct + 3.4) * milk_pta * 0) + (13000 * protpct * 0)) / 100 * 2
    expected = milk + fat + protein + 5.5 * 6 * 2 + 101 * 0.2 * 2 + (-11) * -2.2514
    assert round(fw_index(_JUMPSTART), 4) == round(expected, 4)


def test_fw_ignores_lameness_and_mastitis_total() -> None:
    with_lame = dict(_JUMPSTART)
    without_lame = dict(_JUMPSTART, lameness=0, mastitis=0)
    assert round(fw_index(with_lame), 4) == round(fw_index(without_lame), 4)


def test_attach_custom_indexes_adds_rounded_fields() -> None:
    payload = attach_custom_indexes(dict(_JUMPSTART))
    assert payload["dp_index"] == round(dp_index(_JUMPSTART), 2)
    assert payload["fw_index"] == round(fw_index(_JUMPSTART), 2)


def test_missing_traits_treat_as_zero() -> None:
    assert dp_index({}) == 0
    assert fw_index({}) == 0


def test_custom_settings_change_dp_and_fw() -> None:
    custom = merge_index_settings(
        {
            "include_mastitis": True,
            "dp": {"volume_price": 0, "include_lameness": False},
            "fw": {"include_lameness": True},
        }
    )
    assert dp_index(_JUMPSTART, custom) != dp_index(_JUMPSTART)
    assert fw_index(_JUMPSTART, custom) != fw_index(_JUMPSTART)
    mastitis_on = dp_index(_JUMPSTART, {"include_mastitis": True})
    mastitis_off = dp_index(_JUMPSTART)
    assert round(mastitis_on - mastitis_off, 4) == round((-2) * 4.5 * 2, 4)


def test_index_settings_persist_and_reset() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    assert load_index_settings(db) == DEFAULT_INDEX_SETTINGS
    saved = save_index_settings(db, {"dp": {"fat_price": 9.9}, "include_mastitis": True})
    assert saved["dp"]["fat_price"] == 9.9
    assert saved["include_mastitis"] is True
    assert saved["fw"]["volume_price"] == 40.0
    loaded = load_index_settings(db)
    assert loaded["dp"]["fat_price"] == 9.9
    assert loaded["include_mastitis"] is True
    reset = reset_index_settings(db)
    assert reset == DEFAULT_INDEX_SETTINGS
    assert load_index_settings(db) == DEFAULT_INDEX_SETTINGS
    db.close()
