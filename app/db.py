from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL
from app.models import DEFAULT_BUSINESS, SUPPLIER_WYNNSTAY, Base

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
    _migrate_supplier_schema()
    _migrate_herd_inventory_schema()


def _add_supplier_column(conn, table: str) -> None:
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN supplier VARCHAR(32) DEFAULT 'wynnstay'"))
    conn.execute(text(f"UPDATE {table} SET supplier = 'wynnstay' WHERE supplier IS NULL"))


def _migrate_mapping_options_supplier(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS mapping_options_new (
                id INTEGER PRIMARY KEY,
                supplier VARCHAR(32) NOT NULL DEFAULT 'wynnstay',
                option_type VARCHAR(32) NOT NULL,
                value VARCHAR(255) NOT NULL,
                UNIQUE (supplier, option_type, value)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT OR IGNORE INTO mapping_options_new (id, supplier, option_type, value)
            SELECT id, 'wynnstay', option_type, value FROM mapping_options
            """
        )
    )
    conn.execute(text("DROP TABLE mapping_options"))
    conn.execute(text("ALTER TABLE mapping_options_new RENAME TO mapping_options"))


def _migrate_herd_inventory_schema() -> None:
    inspector = inspect(engine)
    if "herd_inventory" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("herd_inventory")}
    new_columns = {
        "gender": "VARCHAR(8)",
        "aged": "INTEGER",
        "months_old": "INTEGER",
        "fiscal_year_due": "INTEGER",
        "sort_key": "INTEGER",
        "expected_month": "VARCHAR(16)",
        "value": "FLOAT",
    }
    with engine.begin() as conn:
        for name, col_type in new_columns.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE herd_inventory ADD COLUMN {name} {col_type}"))


def _migrate_supplier_schema() -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in ("invoice_lines", "import_batches", "product_mapping_rules"):
            if table not in existing:
                continue
            columns = {col["name"] for col in inspector.get_columns(table)}
            if "supplier" not in columns:
                _add_supplier_column(conn, table)

        if "mapping_options" in existing:
            columns = {col["name"] for col in inspector.get_columns("mapping_options")}
            if "supplier" not in columns:
                if DATABASE_URL.startswith("sqlite"):
                    _migrate_mapping_options_supplier(conn)
                else:
                    _add_supplier_column(conn, "mapping_options")
                    conn.execute(
                        text(
                            "ALTER TABLE mapping_options "
                            "DROP CONSTRAINT IF EXISTS uq_mapping_option_type_value"
                        )
                    )
                    conn.execute(
                        text(
                            "ALTER TABLE mapping_options "
                            "ADD CONSTRAINT uq_mapping_option_supplier_type_value "
                            "UNIQUE (supplier, option_type, value)"
                        )
                    )


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
