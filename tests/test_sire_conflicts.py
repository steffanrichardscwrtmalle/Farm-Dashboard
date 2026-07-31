"""Sire conflict registration matching."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GenomicResult, HerdInventory
from app.services.sire_conflicts import _last12_digits, list_sire_conflicts


def test_last12_digits_ignores_leading_zeros() -> None:
    assert _last12_digits("3244007413") == "3244007413"
    assert _last12_digits("003244007413") == "3244007413"
    assert _last12_digits("UK003244007413") == "3244007413"
    assert _last12_digits("0000") == "0"


def test_list_sire_conflicts_ignores_leading_zero_mismatch() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    session.add(
        HerdInventory(
            farm="CM",
            cow_id="100",
            etag="UK740651324400",
            sreg="3244007413",
        )
    )
    session.add(
        GenomicResult(
            hbn="740651324400",
            eartag="UK740651324400",
            sire_reg="003244007413",
        )
    )
    session.add(
        HerdInventory(
            farm="CM",
            cow_id="101",
            etag="UK740651324401",
            sreg="1111111111",
        )
    )
    session.add(
        GenomicResult(
            hbn="740651324401",
            eartag="UK740651324401",
            sire_reg="2222222222",
        )
    )
    session.commit()

    result = list_sire_conflicts(session, farms=["CM"])
    assert result["count"] == 1
    assert result["rows"][0]["etag"] == "UK740651324401"
    assert result["rows"][0]["sreg"] == "1111111111"
    assert result["rows"][0]["genomic_sreg"] == "2222222222"

    session.close()
