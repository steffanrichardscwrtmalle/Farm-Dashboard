from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL
from app.models import DEFAULT_BUSINESS, SUPPLIER_WYNNSTAY, Base, User

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
    _migrate_cow_events_schema()
    _dedupe_fresh_cow_events()
    _migrate_sales_payments_schema()
    _migrate_herd_births_schema()
    _migrate_stock_accruals_schema()
    _migrate_stock_purchases_schema()
    _migrate_user_permissions()
    _migrate_feedlync_auth()


def _migrate_user_permissions() -> None:
    import json

    from app.auth.permissions import (
        DEFAULT_EDITOR_PERMISSIONS,
        DEFAULT_VIEWER_PERMISSIONS,
        serialize_permissions,
    )
    from app.auth.roles import LEGACY_ROLE_EDITOR, LEGACY_ROLE_VIEWER, ROLE_USER

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "permissions" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN permissions TEXT"))

    from sqlalchemy import select

    with SessionLocal() as db:
        users = list(db.scalars(select(User)).all())
        changed = False
        for user in users:
            if user.role == LEGACY_ROLE_EDITOR:
                user.role = ROLE_USER
                if not user.permissions:
                    user.permissions = serialize_permissions(DEFAULT_EDITOR_PERMISSIONS)
                changed = True
            elif user.role == LEGACY_ROLE_VIEWER:
                user.role = ROLE_USER
                if not user.permissions:
                    user.permissions = serialize_permissions(DEFAULT_VIEWER_PERMISSIONS)
                changed = True
            elif user.role == ROLE_USER and not user.permissions:
                user.permissions = serialize_permissions(DEFAULT_VIEWER_PERMISSIONS)
                changed = True
        if changed:
            db.commit()


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


def _migrate_cow_events_schema() -> None:
    inspector = inspect(engine)
    if "cow_events" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("cow_events")}
    with engine.begin() as conn:
        if "dest" not in columns:
            conn.execute(text("ALTER TABLE cow_events ADD COLUMN dest VARCHAR(128)"))
        if DATABASE_URL.startswith("sqlite"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_cow_events_sold_farm_date "
                    "ON cow_events (event, farm, event_date)"
                )
            )


def _dedupe_fresh_cow_events() -> None:
    """Remove duplicate FRESH / SOLD / DIED rows left in cow_events from older imports."""
    from app.services.herd_events_import import (
        remove_duplicate_exit_cow_events,
        remove_duplicate_fresh_cow_events,
    )

    inspector = inspect(engine)
    if "cow_events" not in inspector.get_table_names():
        return
    with SessionLocal() as db:
        removed = remove_duplicate_fresh_cow_events(db) + remove_duplicate_exit_cow_events(
            db
        )
        if removed:
            db.commit()


def _migrate_sales_payments_schema() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "sales_payment_records" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_sales_payment_natural_key "
                "ON sales_payment_records (farm, cow_id, etag, event_date)"
            )
        )


def _migrate_herd_births_schema() -> None:
    inspector = inspect(engine)
    if "herd_births" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("herd_births")}
    if "category" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE herd_births ADD COLUMN category VARCHAR(16)"))


def _migrate_stock_accruals_schema() -> None:
    """Seed opening baselines once tables exist."""
    from app.models import (
        STOCK_GROUP_BEEF,
        STOCK_GROUP_COWS,
        STOCK_GROUP_YOUNGSTOCK,
        StockOpeningBaseline,
    )

    inspector = inspect(engine)
    if "stock_opening_baselines" not in inspector.get_table_names():
        return

    seeds = [
        ("CM", STOCK_GROUP_COWS, "2024-04-01", 2504),
        ("CM", STOCK_GROUP_YOUNGSTOCK, "2024-04-01", 1782),
        ("GAD", STOCK_GROUP_COWS, "2024-12-01", 851),
        ("GAD", STOCK_GROUP_YOUNGSTOCK, "2024-12-01", 1315),
        ("CM", STOCK_GROUP_BEEF, "2025-04-01", 74),
        ("GAD", STOCK_GROUP_BEEF, "2025-04-01", 13),
    ]

    import datetime as dt

    with SessionLocal() as db:
        from sqlalchemy import func, select

        count = db.scalar(select(func.count()).select_from(StockOpeningBaseline)) or 0
        if count > 0:
            baseline = db.scalar(
                select(StockOpeningBaseline).where(
                    StockOpeningBaseline.farm == "CM",
                    StockOpeningBaseline.stock_group == STOCK_GROUP_YOUNGSTOCK,
                    StockOpeningBaseline.month_start == dt.date(2024, 4, 1),
                )
            )
            if baseline and baseline.opening_count == 1780:
                baseline.opening_count = 1782
                db.commit()
            gad_ys = db.scalar(
                select(StockOpeningBaseline).where(
                    StockOpeningBaseline.farm == "GAD",
                    StockOpeningBaseline.stock_group == STOCK_GROUP_YOUNGSTOCK,
                    StockOpeningBaseline.month_start == dt.date(2024, 12, 1),
                )
            )
            if gad_ys and gad_ys.opening_count == 1319:
                gad_ys.opening_count = 1315
                db.commit()
            beef_seeds = [
                ("CM", STOCK_GROUP_BEEF, dt.date(2025, 4, 1), 74),
                ("GAD", STOCK_GROUP_BEEF, dt.date(2025, 4, 1), 13),
            ]
            for farm, stock_group, month_start, opening in beef_seeds:
                existing = db.scalar(
                    select(StockOpeningBaseline).where(
                        StockOpeningBaseline.farm == farm,
                        StockOpeningBaseline.stock_group == stock_group,
                        StockOpeningBaseline.month_start == month_start,
                    )
                )
                if existing is None:
                    db.add(
                        StockOpeningBaseline(
                            farm=farm,
                            stock_group=stock_group,
                            month_start=month_start,
                            opening_count=opening,
                        )
                    )
            gad_beef = db.scalar(
                select(StockOpeningBaseline).where(
                    StockOpeningBaseline.farm == "GAD",
                    StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
                    StockOpeningBaseline.month_start == dt.date(2025, 4, 1),
                )
            )
            if gad_beef and gad_beef.opening_count == 0:
                gad_beef.opening_count = 13
            cm_beef = db.scalar(
                select(StockOpeningBaseline).where(
                    StockOpeningBaseline.farm == "CM",
                    StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
                    StockOpeningBaseline.month_start == dt.date(2025, 4, 1),
                )
            )
            if cm_beef and cm_beef.opening_count == 0:
                cm_beef.opening_count = 74
            if cm_beef and cm_beef.opening_count == 66:
                cm_beef.opening_count = 74
            db.commit()
            return

        for farm, stock_group, month_iso, opening in seeds:
            db.add(
                StockOpeningBaseline(
                    farm=farm,
                    stock_group=stock_group,
                    month_start=dt.date.fromisoformat(month_iso),
                    opening_count=opening,
                )
            )
        db.commit()


def _migrate_stock_purchases_schema() -> None:
    """Drop legacy manual purchase table; animal-level table is created via metadata."""
    inspector = inspect(engine)
    if "stock_purchase_records" in inspector.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE stock_purchase_records"))


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


def _migrate_feedlync_auth() -> None:
    from app.services.feedlync_auth import seed_refresh_token_from_env

    inspector = inspect(engine)
    if "feedlync_auth" not in inspector.get_table_names():
        return
    with SessionLocal() as db:
        seed_refresh_token_from_env(db)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
