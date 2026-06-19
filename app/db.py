from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL
from app.models import DEFAULT_BUSINESS, Base

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"

if DATABASE_URL.startswith("sqlite"):
    if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
        db_path = DATABASE_URL.replace("sqlite:///", "", 1)
        if not os.path.isabs(db_path):
            db_path = str(_PROJECT_ROOT / db_path)
        DATABASE_URL = f"sqlite:///{Path(db_path).as_posix()}"

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine_kwargs: dict = {"connect_args": connect_args}
if not DATABASE_URL.startswith("sqlite"):
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

_LEGACY_TABLES = ("category_rules", "product_rules")


def init_db() -> None:
    if DATABASE_URL.startswith("sqlite"):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _drop_legacy_tables()
    _migrate_invoice_lines_schema()


def _migrate_invoice_lines_schema() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "invoice_lines" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("invoice_lines")}
    with engine.begin() as conn:
        if "business" not in columns:
            conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN business VARCHAR(100)"))
            conn.execute(
                text("UPDATE invoice_lines SET business = :default WHERE business IS NULL"),
                {"default": DEFAULT_BUSINESS},
            )
        if "credit" not in columns:
            conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN credit VARCHAR(10)"))
        conn.execute(
            text(
                "UPDATE invoice_lines SET credit = 'Yes' "
                "WHERE goods_value IS NOT NULL AND goods_value < 0"
            )
        )
        conn.execute(
            text(
                "UPDATE invoice_lines SET credit = 'No' "
                "WHERE goods_value IS NULL OR goods_value >= 0"
            )
        )


def _drop_legacy_tables() -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    to_drop = [t for t in _LEGACY_TABLES if t in existing]
    if not to_drop:
        return
    with engine.begin() as conn:
        for table in to_drop:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
