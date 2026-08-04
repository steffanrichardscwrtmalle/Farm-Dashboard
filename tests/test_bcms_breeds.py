from app.services.bcms_breeds import bcms_breed_from_cbrd


def test_bcms_breed_from_cbrd_known_codes() -> None:
    assert bcms_breed_from_cbrd(1) == "HF"
    assert bcms_breed_from_cbrd(1.0) == "HF"
    assert bcms_breed_from_cbrd(4) == "JE"
    assert bcms_breed_from_cbrd(19) == "HE"
    assert bcms_breed_from_cbrd(21) == "AA"
    assert bcms_breed_from_cbrd(101) == "HF"
    assert bcms_breed_from_cbrd(119) == "HEX"
    assert bcms_breed_from_cbrd(121) == "AAX"
    assert bcms_breed_from_cbrd(254) == "WAX"


def test_bcms_breed_from_cbrd_unknown() -> None:
    assert bcms_breed_from_cbrd(None) == ""
    assert bcms_breed_from_cbrd(999) == ""
    assert bcms_breed_from_cbrd("x") == ""
