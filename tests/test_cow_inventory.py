"""Cow inventory report grouped by lactation number."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, HerdInventory
from app.services.cow_inventory import (
    build_cow_inventory_csv,
    get_cow_inventory_report,
)


def _add_cow(
    session,
    *,
    farm: str,
    etag: str,
    cow_id: str,
    lact: float,
    category: str = "Dairy",
) -> None:
    session.add(
        HerdInventory(
            farm=farm,
            etag=etag,
            cow_id=cow_id,
            category=category,
            gender="Female",
            months_old=48,
            aged=1440,
            sbrd="HF",
            lact=lact,
        )
    )


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_cow_inventory_groups_by_lact_not_age() -> None:
    session = _session()
    _add_cow(session, farm="CM", etag="UK1", cow_id="1", lact=1)
    _add_cow(session, farm="CM", etag="UK2", cow_id="2", lact=1)
    _add_cow(session, farm="CM", etag="UK3", cow_id="3", lact=3)
    _add_cow(session, farm="GAD", etag="UK4", cow_id="4", lact=2)
    session.commit()

    report = get_cow_inventory_report(session, farms=["CM", "GAD"])
    assert report["lact_bounds"] == {"min": 1, "max": 3}
    assert [row["lact"] for row in report["rows"]] == [1, 2, 3]
    by_lact = {row["lact"]: row for row in report["rows"]}
    assert by_lact[1] == {"lact": 1, "CM": 2, "GAD": 0, "total": 2}
    assert by_lact[2] == {"lact": 2, "CM": 0, "GAD": 1, "total": 1}
    assert by_lact[3] == {"lact": 3, "CM": 1, "GAD": 0, "total": 1}
    assert report["grand_total"] == {"CM": 3, "GAD": 1, "total": 4}
    assert report["range_summary"]["lact_count"] == 3
    assert report["range_summary"]["average_per_lactation"] == 1.3


def test_cow_inventory_excludes_heifers_and_beef() -> None:
    session = _session()
    _add_cow(session, farm="CM", etag="UK1", cow_id="1", lact=2)
    _add_cow(session, farm="CM", etag="UK2", cow_id="2", lact=0, category="Youngstock")
    session.add(
        HerdInventory(
            farm="CM",
            etag="UK3",
            cow_id="3",
            category="Beef",
            gender="Male",
            months_old=12,
            aged=360,
            sbrd="AA",
            lact=0,
        )
    )
    session.commit()

    report = get_cow_inventory_report(session, farms=["CM"])
    assert report["grand_total"]["CM"] == 1
    assert report["rows"][0]["lact"] == 2


def test_cow_inventory_lact_range_and_farm_filter() -> None:
    session = _session()
    _add_cow(session, farm="CM", etag="UK1", cow_id="1", lact=1)
    _add_cow(session, farm="CM", etag="UK2", cow_id="2", lact=4)
    _add_cow(session, farm="GAD", etag="UK3", cow_id="3", lact=2)
    session.commit()

    cm_only = get_cow_inventory_report(session, farms=["CM"], min_lact=1, max_lact=2)
    assert cm_only["grand_total"] == {"CM": 1, "GAD": 0, "total": 1}
    assert [row["lact"] for row in cm_only["rows"]] == [1, 2]
    assert cm_only["lact_bounds"] == {"min": 1, "max": 4}

    csv_text = build_cow_inventory_csv(cm_only, ["CM"])
    assert csv_text.splitlines()[0] == "LACT,CM,Total"
    assert "Grand Total,1,1" in csv_text
