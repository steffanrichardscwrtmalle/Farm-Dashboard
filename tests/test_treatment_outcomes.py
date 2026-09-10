from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CowEvent, HerdInventory, SenseHubYoungstockHealth
from app.services.sensehub_youngstock import classify_antibiotic
from app.services.treatment_outcomes import (
    age_band_for,
    daily_averages,
    days_to_recover,
    breed_for,
    default_month_range,
    episode_start_dates,
    event_count_label,
    parse_fiscal_year,
    recovered_by_day,
    starting_band_for,
    treatment_outcomes,
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _health(
    animal_id: str,
    day: dt.date,
    value: float,
    *,
    slot: str = "midday",
    hour: int | None = None,
    age_days: int | None = None,
) -> SenseHubYoungstockHealth:
    hours = {"midnight": 0, "6am": 6, "midday": 12, "6pm": 18, "live": 15}
    return SenseHubYoungstockHealth(
        animal_id=animal_id,
        sampled_at=dt.datetime(day.year, day.month, day.day, hour if hour is not None else hours[slot]),
        slot=slot,
        health_index=value,
        age_days=age_days,
    )


def _add_days(
    session: Session,
    animal_id: str,
    start: dt.date,
    values: dict[int, float],
    *,
    age_on_start: int | None = None,
) -> None:
    for offset, value in values.items():
        day = start + dt.timedelta(days=offset)
        age = None if age_on_start is None else age_on_start + offset
        for slot in ("midnight", "6am", "midday", "6pm"):
            session.add(_health(animal_id, day, value, slot=slot, age_days=age))


def test_episode_start_dates_collapse_seven_day_repeats() -> None:
    starts = episode_start_dates(
        [
            dt.date(2026, 8, 1),
            dt.date(2026, 8, 3),
            dt.date(2026, 8, 8),
            dt.date(2026, 8, 16),
        ]
    )
    assert starts == [dt.date(2026, 8, 1), dt.date(2026, 8, 16)]


def test_daily_averages_ignore_live_slot() -> None:
    day = dt.date(2026, 8, 1)
    samples = [
        _health("435259", day, 80, slot="midnight"),
        _health("435259", day, 82, slot="6am"),
        _health("435259", day, 84, slot="midday"),
        _health("435259", day, 86, slot="6pm"),
        _health("435259", day, 50, slot="live", hour=15),
    ]
    daily = daily_averages(samples, day)
    assert daily[0] == 83.0


def test_days_to_recover_needs_two_consecutive_days() -> None:
    daily = {0: 80.0, 1: 85.0, 2: 84.0, 3: 85.0, 4: 86.0}
    assert days_to_recover(daily) == 3
    assert recovered_by_day(daily, 7) is True
    assert days_to_recover({0: 85.0, 1: 84.0}) is None


def test_starting_and_age_bands() -> None:
    assert starting_band_for(79.9) == "low"
    assert starting_band_for(84.0) == "watch"
    assert starting_band_for(85.0) == "moderate"
    assert starting_band_for(90.0) == "healthy"
    assert age_band_for(10) == "under_3w"
    assert age_band_for(21) == "3_to_8w"
    assert age_band_for(56) == "over_8w"


def test_classify_antibiotic_prefers_draxxin() -> None:
    draxxin = CowEvent(farm="CM", cow_id="1", event="RESP", remark="DRAXXIN")
    fenflor = CowEvent(farm="CM", cow_id="1", event="RESP", protocols="Fenflor")
    other = CowEvent(farm="CM", cow_id="1", event="RESP", remark="Nuflor")
    assert classify_antibiotic([fenflor, draxxin]) == "draxxin"
    assert classify_antibiotic([fenflor]) == "fenflor"
    assert classify_antibiotic([other]) == "other"


def test_treatment_outcomes_recovery_curve_and_unrecovered() -> None:
    session = _db()
    start = dt.date(2026, 7, 1)
    today = dt.date(2026, 9, 10)

    session.add(HerdInventory(farm="CM", cow_id="111111", etag="UK0000111111", bdat=dt.date(2026, 5, 20)))
    session.add(HerdInventory(farm="CM", cow_id="222222", etag="UK0000222222", bdat=dt.date(2026, 6, 1)))
    session.add(HerdInventory(farm="CM", cow_id="333333", etag="UK0000333333", bdat=dt.date(2026, 6, 10)))
    session.add(HerdInventory(farm="CM", cow_id="444444", etag="UK0000444444", bdat=dt.date(2026, 6, 15)))

    session.add(CowEvent(farm="CM", cow_id="111111", event="RESP", event_date=start, remark="DRAXXIN"))
    session.add(CowEvent(farm="CM", cow_id="111111", event="RESP", event_date=start + dt.timedelta(days=2), remark="DRAXXIN"))
    _add_days(
        session,
        "111111",
        start,
        {offset: 78.0 if offset < 4 else 86.0 for offset in range(-2, 15)},
        age_on_start=42,
    )

    session.add(CowEvent(farm="CM", cow_id="222222", event="RESP", event_date=start, remark="FENFLOR"))
    _add_days(session, "222222", start, {offset: 76.0 for offset in range(-2, 15)}, age_on_start=30)

    session.add(CowEvent(farm="CM", cow_id="333333", event="RESP", event_date=start, remark="LOXICOM"))
    _add_days(session, "333333", start, {offset: 70.0 for offset in range(-2, 15)}, age_on_start=21)

    session.add(CowEvent(farm="CM", cow_id="444444", event="RESP", event_date=start, remark="DRAXXIN"))
    session.add(CowEvent(farm="CM", cow_id="444444", event="DIED", event_date=start + dt.timedelta(days=3), remark="PNEU"))
    _add_days(session, "444444", start, {offset: 72.0 for offset in range(-2, 4)}, age_on_start=16)

    session.commit()
    payload = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start + dt.timedelta(days=7),
        min_episodes=0,
        today=today,
    )

    assert payload["episode_count"] == 3
    products = {row["product"]: row for row in payload["summary"]}
    assert products["draxxin"]["episodes"] == 2
    assert products["draxxin"]["median_days_to_recover"] == 4
    assert products["draxxin"]["recovered_by_day_7"] == 1
    assert products["draxxin"]["recovered_by_day_7_eligible"] == 1
    assert products["draxxin"]["recovered_by_day_7_pct"] == 100.0
    assert products["draxxin"]["left"] == 1
    assert products["fenflor"]["recovered_by_day_14_pct"] == 0.0
    assert products["fenflor"]["relapsed_pct"] == 0.0

    unrecovered_ids = {row["animal_id"] for row in payload["unrecovered"]}
    assert unrecovered_ids == {"222222"}

    draxxin_series = next(item for item in payload["curve"]["series"] if item["product"] == "draxxin")
    assert draxxin_series["means"][2] == 75.0
    assert draxxin_series["counts"][6] == 1


def test_treatment_outcomes_relapse_and_recent_not_unrecovered() -> None:
    session = _db()
    first = dt.date(2026, 7, 1)
    second = dt.date(2026, 7, 16)
    recent = dt.date(2026, 9, 5)
    today = dt.date(2026, 9, 10)

    session.add(HerdInventory(farm="GAD", cow_id="555555", etag="555555", bdat=dt.date(2026, 5, 1)))
    session.add(HerdInventory(farm="GAD", cow_id="666666", etag="666666", bdat=dt.date(2026, 6, 20)))
    session.add(CowEvent(farm="GAD", cow_id="555555", event="RESP", event_date=first, remark="DRAXXIN"))
    session.add(CowEvent(farm="GAD", cow_id="555555", event="RESP", event_date=second, remark="DRAXXIN"))
    session.add(CowEvent(farm="GAD", cow_id="666666", event="RESP", event_date=recent, remark="FENFLOR"))
    span_days = (second - first).days + 15
    _add_days(
        session,
        "555555",
        first,
        {offset: 86.0 if offset >= 2 else 80.0 for offset in range(-2, span_days)},
    )
    _add_days(session, "666666", recent, {offset: 74.0 for offset in range(-2, 6)})
    session.commit()

    payload = treatment_outcomes(
        session,
        farms=["GAD"],
        date_from=dt.date(2026, 7, 1),
        date_to=today,
        min_episodes=0,
        today=today,
    )
    by_id = {(row["animal_id"], row["event_date"]): row for row in payload["unrecovered"]}
    assert ("666666", recent.isoformat()) not in by_id
    summary = next(row for row in payload["summary"] if row["product"] == "draxxin")
    assert summary["episodes"] == 2
    assert summary["relapsed"] == 1
    assert summary["relapsed_eligible"] == 2


def test_treatment_outcomes_starting_band_filter() -> None:
    session = _db()
    start = dt.date(2026, 7, 1)
    today = dt.date(2026, 9, 10)
    session.add(HerdInventory(farm="CM", cow_id="777777", etag="777777", bdat=dt.date(2026, 5, 1)))
    session.add(CowEvent(farm="CM", cow_id="777777", event="RESP", event_date=start, remark="DRAXXIN"))
    _add_days(session, "777777", start, {offset: 92.0 if offset < 0 else 88.0 for offset in range(-2, 15)})
    session.commit()

    all_rows = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        min_episodes=0,
        today=today,
    )
    low_only = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        starting_band="low",
        min_episodes=0,
        today=today,
    )
    healthy = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        starting_band="healthy",
        min_episodes=0,
        today=today,
    )
    assert all_rows["episode_count"] == 1
    assert low_only["episode_count"] == 0
    assert healthy["episode_count"] == 1
    assert healthy["summary"][0]["recovered_by_day_7_pct"] == 100.0


def test_parse_fiscal_year_and_default_month_range() -> None:
    assert parse_fiscal_year(None) == (None, False)
    assert parse_fiscal_year("any") == (None, True)
    assert parse_fiscal_year("2027") == (2027, False)
    start, end = default_month_range(
        dt.date(2026, 4, 1),
        dt.date(2027, 3, 31),
        today=dt.date(2026, 9, 10),
    )
    assert start == dt.date(2026, 7, 1)
    assert end == dt.date(2026, 9, 30)


def test_treatment_outcomes_defaults_last_three_months_and_latest_year() -> None:
    session = _db()
    today = dt.date(2026, 9, 10)
    treated = dt.date(2026, 8, 1)
    session.add(HerdInventory(farm="CM", cow_id="888888", etag="888888", bdat=dt.date(2026, 5, 1)))
    session.add(
        CowEvent(
            farm="CM",
            cow_id="888888",
            event="RESP",
            event_date=treated,
            remark="DRAXXIN",
            fiscal_year=2027,
        )
    )
    _add_days(session, "888888", treated, {offset: 86.0 for offset in range(-2, 15)})
    session.commit()

    payload = treatment_outcomes(session, farms=["CM"], min_episodes=0, today=today)
    assert payload["fiscal_year"] == 2027
    assert payload["date_from"] == "2026-07-01"
    assert payload["date_to"] == "2026-09-30"
    assert payload["date_bounds"] == {"min": "2026-04-01", "max": "2027-03-31"}
    assert payload["episode_count"] == 1

    any_year = treatment_outcomes(
        session,
        farms=["CM"],
        fiscal_year="any",
        min_episodes=0,
        today=today,
    )
    assert any_year["fiscal_year"] is None
    assert any_year["date_from"] == "2026-08-01"
    assert any_year["date_to"] == "2026-09-10"


def test_treatment_outcomes_defaults_to_products_over_fifty() -> None:
    session = _db()
    start = dt.date(2026, 8, 1)
    today = dt.date(2026, 9, 10)
    session.add(HerdInventory(farm="CM", cow_id="999999", etag="999999", bdat=dt.date(2026, 5, 1)))
    session.add(CowEvent(farm="CM", cow_id="999999", event="RESP", event_date=start, remark="DRAXXIN"))
    _add_days(session, "999999", start, {offset: 86.0 for offset in range(-2, 15)})
    session.commit()

    payload = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        today=today,
    )
    assert payload["episode_count"] == 0
    counts = {item["id"]: item for item in payload["product_counts"]}
    assert counts["draxxin"]["episodes"] == 1
    assert counts["draxxin"]["selected"] is False

    shown = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        products=["draxxin"],
        today=today,
    )
    assert shown["episode_count"] == 1


def test_event_count_label() -> None:
    assert event_count_label(1) == "1st treatment"
    assert event_count_label(2) == "2nd treatment"
    assert event_count_label(3) == "3rd treatment"
    assert event_count_label(4) == "4th treatment"
    assert event_count_label(11) == "11th treatment"
    assert event_count_label(21) == "21st treatment"


def test_treatment_outcomes_filters_first_versus_second_episode() -> None:
    session = _db()
    first = dt.date(2026, 5, 1)
    second = dt.date(2026, 8, 1)
    today = dt.date(2026, 9, 10)
    session.add(HerdInventory(farm="CM", cow_id="101010", etag="101010", bdat=dt.date(2026, 4, 1)))
    session.add(CowEvent(farm="CM", cow_id="101010", event="RESP", event_date=first, remark="FENFLOR"))
    session.add(CowEvent(farm="CM", cow_id="101010", event="RESP", event_date=second, remark="DRAXXIN"))
    _add_days(session, "101010", first, {offset: 86.0 for offset in range(-2, 15)})
    _add_days(session, "101010", second, {offset: 80.0 for offset in range(0, 15)})
    session.commit()

    window = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=dt.date(2026, 7, 1),
        date_to=dt.date(2026, 9, 30),
        min_episodes=0,
        today=today,
    )
    assert window["episode_count"] == 1
    options = {item["id"]: item for item in window["event_count_options"]}
    assert 2 in options
    assert options[2]["label"] == "2nd treatment"
    assert window["unrecovered"][0]["treatment_number"] == 2

    first_only = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=dt.date(2026, 7, 1),
        date_to=dt.date(2026, 9, 30),
        event_counts=[1],
        min_episodes=0,
        today=today,
    )
    assert first_only["episode_count"] == 0

    second_only = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=dt.date(2026, 7, 1),
        date_to=dt.date(2026, 9, 30),
        event_counts=[2],
        min_episodes=0,
        today=today,
    )
    assert second_only["episode_count"] == 1


def test_breed_for_uses_inventory_category_then_cbrd() -> None:
    dairy = HerdInventory(farm="CM", cow_id="1", category="Youngstock")
    beef = HerdInventory(farm="CM", cow_id="2", category="Beef")
    by_cbrd = HerdInventory(farm="CM", cow_id="3", cbrd=80, gender="F")
    assert breed_for(dairy) == "dairy"
    assert breed_for(beef) == "beef"
    assert breed_for(by_cbrd) == "dairy"
    assert breed_for(None, [CowEvent(farm="CM", cow_id="4", event="RESP", cbrd=120, gndr="M")]) == "beef"


def test_treatment_outcomes_filters_dairy_versus_beef() -> None:
    session = _db()
    start = dt.date(2026, 8, 1)
    today = dt.date(2026, 9, 10)
    session.add(HerdInventory(farm="CM", cow_id="121212", etag="121212", category="Dairy", bdat=dt.date(2026, 5, 1)))
    session.add(HerdInventory(farm="CM", cow_id="131313", etag="131313", category="Beef", bdat=dt.date(2026, 5, 1)))
    session.add(CowEvent(farm="CM", cow_id="121212", event="RESP", event_date=start, remark="DRAXXIN"))
    session.add(CowEvent(farm="CM", cow_id="131313", event="RESP", event_date=start, remark="DRAXXIN"))
    _add_days(session, "121212", start, {offset: 86.0 for offset in range(-2, 15)})
    _add_days(session, "131313", start, {offset: 86.0 for offset in range(-2, 15)})
    session.commit()

    default = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        min_episodes=0,
        today=today,
    )
    both = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        breeds=["dairy", "beef"],
        min_episodes=0,
        today=today,
    )
    dairy_only = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        breeds=["dairy"],
        min_episodes=0,
        today=today,
    )
    beef_only = treatment_outcomes(
        session,
        farms=["CM"],
        date_from=start,
        date_to=start,
        breeds=["beef"],
        min_episodes=0,
        today=today,
    )
    assert default["episode_count"] == 1
    assert default["breeds"] == ["dairy"]
    assert both["episode_count"] == 2
    assert dairy_only["episode_count"] == 1
    assert beef_only["episode_count"] == 1
    assert dairy_only["breeds"] == ["dairy"]
    assert dairy_only["summary"][0]["episodes"] == 1
    assert beef_only["summary"][0]["episodes"] == 1
