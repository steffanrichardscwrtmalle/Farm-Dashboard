"""Tests for HR job-title settings."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.services.hr_service import (
    HRServiceError,
    add_job_title,
    list_job_titles,
    remove_job_title,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_list_job_titles_seeds_default(db):
    assert list_job_titles(db) == ["Farm Worker"]
    # Second call reads persisted setting.
    assert list_job_titles(db) == ["Farm Worker"]


def test_add_and_remove_job_title(db):
    titles = add_job_title(db, "  Herdsman  ")
    assert titles == ["Farm Worker", "Herdsman"]
    titles = add_job_title(db, "Relief Milker")
    assert "Relief Milker" in titles
    with pytest.raises(HRServiceError, match="already exists"):
        add_job_title(db, "herdsman")
    remaining = remove_job_title(db, "Herdsman")
    assert "Herdsman" not in remaining
    assert "Farm Worker" in remaining


def test_cannot_remove_last_job_title(db):
    list_job_titles(db)
    with pytest.raises(HRServiceError, match="At least one"):
        remove_job_title(db, "Farm Worker")
