"""Farm Schedule recurring jobs."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, FARM_JOB_STATUS_ARCHIVED, FARM_JOB_STATUS_PENDING
from app.services.farm_schedule import (
    complete_job,
    create_job,
    deactivate_template,
    due_counts,
    list_schedule,
    update_job,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def test_create_job_appears_on_pending_list(db: Session) -> None:
    created = create_job(
        db,
        farm="CM",
        name="Change liners",
        due_date="2026-08-16",
        interval_days=42,
    )
    assert created["name"] == "Change liners"
    assert created["due_date"] == "2026-08-16"
    assert created["interval_days"] == 42
    assert created["status"] == FARM_JOB_STATUS_PENDING

    listed = list_schedule(db, farm="CM", view="pending", as_of=dt.date(2026, 8, 16))
    assert listed["total"] == 1
    assert listed["rows"][0]["name"] == "Change liners"
    assert listed["rows"][0]["due_today"] is True
    assert listed["farm_label"] == "Cwrt Malle"


def test_complete_archives_and_schedules_next(db: Session) -> None:
    created = create_job(
        db,
        farm="GAD",
        name="Grease parlour",
        due_date=dt.date(2026, 7, 1),
        interval_days=42,
    )
    result = complete_job(
        db,
        farm="GAD",
        occurrence_id=created["id"],
        completed_on="2026-07-03",
        completed_by="Steff",
        as_of=dt.date(2026, 7, 3),
    )
    assert result["completed"]["status"] == FARM_JOB_STATUS_ARCHIVED
    assert result["completed"]["completed_by"] == "Steff"
    assert result["completed"]["completed_on"] == "2026-07-03"
    assert result["next"]["due_date"] == "2026-08-14"
    assert result["next"]["status"] == FARM_JOB_STATUS_PENDING

    pending = list_schedule(db, farm="GAD", view="pending", as_of=dt.date(2026, 8, 14))
    archive = list_schedule(db, farm="GAD", view="archive", as_of=dt.date(2026, 8, 14))
    assert pending["total"] == 1
    assert pending["rows"][0]["due_date"] == "2026-08-14"
    assert archive["total"] == 1
    assert archive["rows"][0]["completed_by"] == "Steff"


def test_jobs_are_farm_specific(db: Session) -> None:
    create_job(db, farm="CM", name="CM only", due_date="2026-08-01", interval_days=7)
    create_job(db, farm="GAD", name="GAD only", due_date="2026-08-01", interval_days=7)
    cm = list_schedule(db, farm="CM", view="pending")
    gad = list_schedule(db, farm="GAD", view="pending")
    assert [row["name"] for row in cm["rows"]] == ["CM only"]
    assert [row["name"] for row in gad["rows"]] == ["GAD only"]


def test_deactivate_removes_pending_keeps_archive(db: Session) -> None:
    created = create_job(
        db, farm="CM", name="Oil change", due_date="2026-08-01", interval_days=30
    )
    complete_job(
        db,
        farm="CM",
        occurrence_id=created["id"],
        completed_on="2026-08-01",
        completed_by="Aled",
    )
    pending = list_schedule(db, farm="CM", view="pending")
    template_id = pending["rows"][0]["template_id"]
    deactivate_template(db, farm="CM", template_id=template_id)
    assert list_schedule(db, farm="CM", view="pending")["total"] == 0
    assert list_schedule(db, farm="CM", view="archive")["total"] == 1


def test_cannot_complete_twice(db: Session) -> None:
    created = create_job(
        db, farm="CM", name="Wash plant", due_date="2026-08-01", interval_days=7
    )
    complete_job(
        db,
        farm="CM",
        occurrence_id=created["id"],
        completed_on="2026-08-01",
        completed_by="Steff",
    )
    with pytest.raises(ValueError, match="already completed"):
        complete_job(
            db,
            farm="CM",
            occurrence_id=created["id"],
            completed_on="2026-08-02",
            completed_by="Steff",
        )


def test_create_rejects_blank_name_and_zero_interval(db: Session) -> None:
    with pytest.raises(ValueError, match="Job name"):
        create_job(db, farm="CM", name="  ", due_date="2026-08-01", interval_days=7)
    with pytest.raises(ValueError, match="interval"):
        create_job(db, farm="CM", name="Liners", due_date="2026-08-01", interval_days=0)


def test_update_job_changes_name_due_and_interval(db: Session) -> None:
    created = create_job(
        db,
        farm="CM",
        name="Change liners",
        due_date="2026-08-16",
        interval_days=42,
        notes="Old note",
    )
    updated = update_job(
        db,
        farm="CM",
        occurrence_id=created["id"],
        name="Liner change",
        due_date="2026-08-20",
        interval_days=50,
        notes="New note",
    )
    assert updated["name"] == "Liner change"
    assert updated["due_date"] == "2026-08-20"
    assert updated["interval_days"] == 50
    assert updated["notes"] == "New note"
    listed = list_schedule(db, farm="CM", view="pending")
    assert listed["rows"][0]["name"] == "Liner change"
    assert listed["rows"][0]["due_date"] == "2026-08-20"


def test_due_counts_includes_today_and_overdue_only(db: Session) -> None:
    today = dt.date(2026, 8, 16)
    create_job(db, farm="CM", name="Due today", due_date=today, interval_days=7)
    create_job(
        db, farm="CM", name="Future", due_date=today + dt.timedelta(days=3), interval_days=7
    )
    create_job(
        db, farm="GAD", name="Overdue", due_date=today - dt.timedelta(days=2), interval_days=7
    )
    result = due_counts(db, as_of=today)
    assert result["counts"]["CM"] == 1
    assert result["counts"]["GAD"] == 1
