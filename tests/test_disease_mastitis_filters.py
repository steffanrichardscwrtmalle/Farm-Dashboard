from app.services.events_common import (
    DISEASE_EVENT_LABELS,
    DISEASE_FILTER_OPTIONS,
    disease_db_event_types,
    is_loxicom_only_mastitis_remark,
    normalize_disease_type,
    resolve_page_event_types,
)


def test_mastitis_filter_options_include_all_and_abx() -> None:
    assert "MAST" in DISEASE_FILTER_OPTIONS
    assert "MAST_ABX" in DISEASE_FILTER_OPTIONS
    assert "MAST_LOX" not in DISEASE_FILTER_OPTIONS
    assert DISEASE_FILTER_OPTIONS.index("MAST_ABX") == DISEASE_FILTER_OPTIONS.index("MAST") + 1
    assert DISEASE_EVENT_LABELS["MAST"] == "Mastitis (all)"
    assert DISEASE_EVENT_LABELS["MAST_ABX"] == "Mastitis - Abx"


def test_normalize_and_resolve_mastitis_filters() -> None:
    assert normalize_disease_type("mast_abx") == "MAST_ABX"
    assert resolve_page_event_types("disease", "MAST") == ("MAST",)
    assert resolve_page_event_types("disease", "MAST_ABX") == ("MAST_ABX",)
    assert disease_db_event_types(("MAST_ABX",)) == ("MAST",)
    assert disease_db_event_types(("MAST", "MAST_ABX")) == ("MAST",)


def test_loxicom_only_remark_match() -> None:
    assert is_loxicom_only_mastitis_remark("LOXICOM") is True
    assert is_loxicom_only_mastitis_remark(" loxicom ") is True
    assert is_loxicom_only_mastitis_remark("RECOCAM") is False
    assert is_loxicom_only_mastitis_remark("SYNULXBR") is False
    assert is_loxicom_only_mastitis_remark(None) is False
