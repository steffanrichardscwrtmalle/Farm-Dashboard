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
    _migrate_pedigree_registration_schema()
    _migrate_genomic_results_schema()
    _migrate_nml_results_schema()
    _migrate_milk_collections_schema()
    _migrate_milk_statements_schema()
    _migrate_cattle_sales_schema()
    _migrate_cow_events_schema()
    _dedupe_fresh_cow_events()
    _migrate_sales_payments_schema()
    _migrate_fallen_stock_schema()
    _migrate_herd_births_schema()
    _migrate_stock_accruals_schema()
    _migrate_stock_purchases_schema()
    _migrate_user_permissions()
    _migrate_feedlync_auth()
    _migrate_hr_schema()


def _migrate_user_permissions() -> None:
    import json

    from app.auth.permissions import (
        DEFAULT_EDITOR_PERMISSIONS,
        DEFAULT_VIEWER_PERMISSIONS,
        ACTION_CATTLE_SALES_IMPORT,
        ACTION_MILK_COLLECTIONS_IMPORT,
        ACTION_MILK_QUALITY_IMPORT,
        ACTION_MILK_STATEMENTS_IMPORT,
        PAGE_CATTLE_SALES,
        PAGE_MILK_QUALITY,
        parse_permissions,
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
            elif user.role == ROLE_USER and user.permissions:
                perms = parse_permissions(user.permissions)
                actions = list(perms.get("actions", []))
                if ACTION_MILK_STATEMENTS_IMPORT not in actions and (
                    ACTION_MILK_QUALITY_IMPORT in actions
                    or ACTION_MILK_COLLECTIONS_IMPORT in actions
                ):
                    actions.append(ACTION_MILK_STATEMENTS_IMPORT)
                    perms["actions"] = sorted(set(actions))
                    user.permissions = serialize_permissions(perms)
                    changed = True
                pages = list(perms.get("pages", []))
                if PAGE_CATTLE_SALES not in pages and PAGE_MILK_QUALITY in pages:
                    pages.append(PAGE_CATTLE_SALES)
                    perms["pages"] = sorted(set(pages))
                    user.permissions = serialize_permissions(perms)
                    changed = True
                if ACTION_CATTLE_SALES_IMPORT not in actions and (
                    ACTION_MILK_QUALITY_IMPORT in actions
                    or ACTION_MILK_COLLECTIONS_IMPORT in actions
                    or ACTION_MILK_STATEMENTS_IMPORT in actions
                ):
                    actions.append(ACTION_CATTLE_SALES_IMPORT)
                    perms["actions"] = sorted(set(actions))
                    user.permissions = serialize_permissions(perms)
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
        "ped": "INTEGER",
        "dped": "INTEGER",
        "dreg": "VARCHAR(64)",
        "sreg": "VARCHAR(64)",
        "sid": "VARCHAR(64)",
        "gid": "VARCHAR(64)",
        "gtest": "DATE",
        "subd": "DATE",
    }
    with engine.begin() as conn:
        for name, col_type in new_columns.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE herd_inventory ADD COLUMN {name} {col_type}"))


def _migrate_pedigree_registration_schema() -> None:
    inspector = inspect(engine)
    if "pedigree_registration_records" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("pedigree_registration_records")}
    new_columns = {"sid": "VARCHAR(64)"}
    with engine.begin() as conn:
        for name, col_type in new_columns.items():
            if name not in columns:
                conn.execute(
                    text(
                        f"ALTER TABLE pedigree_registration_records ADD COLUMN {name} {col_type}"
                    )
                )


def _migrate_genomic_results_schema() -> None:
    inspector = inspect(engine)
    if "genomic_results" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("genomic_results")}
    new_columns = {"sire_reg": "VARCHAR(64)"}
    with engine.begin() as conn:
        for name, col_type in new_columns.items():
            if name not in columns:
                conn.execute(
                    text(f"ALTER TABLE genomic_results ADD COLUMN {name} {col_type}")
                )


def _migrate_nml_results_schema() -> None:
    """nml_milk_results is created via metadata; ensure helpful indexes exist."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "nml_milk_results" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_nml_farm_sample_date "
                "ON nml_milk_results (farm, sample_date)"
            )
        )


def _migrate_milk_collections_schema() -> None:
    """milk_collections is created via metadata; add later columns/indexes."""
    inspector = inspect(engine)
    if "milk_collections" not in inspector.get_table_names():
        return
    columns = {col["name"]: col for col in inspector.get_columns("milk_collections")}
    with engine.begin() as conn:
        if "source_received" not in columns:
            conn.execute(
                text("ALTER TABLE milk_collections ADD COLUMN source_received TIMESTAMP")
            )

    # Sample numbers may be blank; make the column nullable so blanks store as
    # NULL (NULLs are distinct in the unique constraint, so several sample-less
    # loads can share a day).
    sample_col = columns.get("sample_id")
    if sample_col is not None and not sample_col.get("nullable", True):
        if DATABASE_URL.startswith("sqlite"):
            _rebuild_milk_collections_sqlite()
        else:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE milk_collections ALTER COLUMN sample_id DROP NOT NULL")
                )

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE milk_collections SET sample_id = NULL "
                "WHERE sample_id IS NOT NULL AND TRIM(sample_id) = ''"
            )
        )
        if DATABASE_URL.startswith("sqlite"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_milk_collections_farm_sample "
                    "ON milk_collections (farm, sample_id)"
                )
            )
    _dedupe_milk_collections()


def _rebuild_milk_collections_sqlite() -> None:
    """Recreate milk_collections so sample_id is nullable (SQLite can't ALTER)."""
    from app.models import MilkCollection

    copy_cols = (
        "id",
        "farm",
        "collection_date",
        "driver",
        "vehicle_reg",
        "arrival_time",
        "depart_time",
        "volume_litres",
        "temp_c",
        "temp_raw",
        "source_message_id",
        "source_file",
        "source_received",
        "imported_at",
    )
    col_list = ", ".join(copy_cols)
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE milk_collections RENAME TO milk_collections_old")
        )
    MilkCollection.__table__.create(bind=engine)
    with engine.begin() as conn:
        # NULLIF turns blank sample numbers into NULL during the copy.
        conn.execute(
            text(
                f"INSERT INTO milk_collections (sample_id, {col_list}) "
                f"SELECT NULLIF(TRIM(sample_id), ''), {col_list} "
                "FROM milk_collections_old"
            )
        )
        conn.execute(text("DROP TABLE milk_collections_old"))


def _migrate_milk_statements_schema() -> None:
    """milk_statements is created via metadata; ensure helpful indexes exist."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "milk_statements" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_milk_statements_farm_month "
                "ON milk_statements (farm, statement_month)"
            )
        )


def _migrate_cattle_sales_schema() -> None:
    """cattle_sale_lines is created via metadata; ensure helpful indexes exist."""
    inspector = inspect(engine)
    if "cattle_sale_lines" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("cattle_sale_lines")}
    with engine.begin() as conn:
        if "reject_kg" not in columns:
            conn.execute(text("ALTER TABLE cattle_sale_lines ADD COLUMN reject_kg FLOAT"))
        if "kill_date" not in columns:
            conn.execute(text("ALTER TABLE cattle_sale_lines ADD COLUMN kill_date DATE"))
        if DATABASE_URL.startswith("sqlite"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_cattle_sale_farm_date "
                    "ON cattle_sale_lines (farm, sale_date)"
                )
            )


def _dedupe_milk_collections() -> None:
    """Collapse each (farm, month) to its most recent email's data."""
    from sqlalchemy import select

    from app.models import MilkCollection
    from app.services.haulier_import import _dedupe_month_emails

    inspector = inspect(engine)
    if "milk_collections" not in inspector.get_table_names():
        return
    with SessionLocal() as db:
        farms = set(db.scalars(select(MilkCollection.farm).distinct()).all())
        removed = _dedupe_month_emails(db, {f for f in farms if f})
        if removed:
            db.commit()


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


def _migrate_fallen_stock_schema() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "fallen_stock_records" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_fallen_stock_natural_key "
                "ON fallen_stock_records (farm, cow_id, etag, event_date)"
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


def _beef_baseline_start_month(farm: str):
    """Beef accruals start with dairy cows (CM Apr-24, GAD Dec-24)."""
    import datetime as dt

    if farm == "GAD":
        return dt.date(2024, 12, 1)
    return dt.date(2024, 4, 1)


def _beef_net_change_between(
    db: Session,
    *,
    farm: str,
    month_from: dt.date,
    month_to: dt.date,
) -> int:
    """Net beef movement from month_from through month_to (inclusive)."""
    from app.models import STOCK_GROUP_BEEF
    from app.services.events_common import _iter_month_starts
    from app.services.stock_accruals import (
        _ZERO_SALES,
        _fetch_beef_births_by_month,
        _fetch_event_count_by_month,
        _fetch_purchases_by_month,
        _fetch_sales_by_month,
        _last_day_of_month,
        _month_key,
    )

    calc_end = _last_day_of_month(month_to)
    sales = _fetch_sales_by_month(
        db, farm=farm, stock_group=STOCK_GROUP_BEEF, month_from=month_from, month_to=calc_end
    )
    deaths = _fetch_event_count_by_month(
        db,
        farm=farm,
        stock_group=STOCK_GROUP_BEEF,
        event_type="DIED",
        month_from=month_from,
        month_to=calc_end,
    )
    purchases = _fetch_purchases_by_month(
        db,
        farm=farm,
        stock_group=STOCK_GROUP_BEEF,
        month_from=month_from,
        month_to=calc_end,
    )
    births = _fetch_beef_births_by_month(
        db, farm=farm, month_from=month_from, month_to=calc_end
    )
    total = 0
    for month_start in _iter_month_starts(month_from, month_to):
        key = _month_key(month_start)
        sales_total = sum(sales.get(key, dict(_ZERO_SALES)).values())
        total += (
            births.get(key, 0)
            + purchases.get(key, 0)
            - sales_total
            - deaths.get(key, 0)
        )
    return total


def _extend_beef_opening_baselines(db: Session) -> None:
    """Move beef baselines back to CM Apr-24 / GAD Dec-24, preserving later openings."""
    from app.models import STOCK_GROUP_BEEF, StockOpeningBaseline

    import datetime as dt

    from sqlalchemy import select

    for farm in ("CM", "GAD"):
        beef = db.scalar(
            select(StockOpeningBaseline).where(
                StockOpeningBaseline.farm == farm,
                StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
            )
        )
        if beef is None:
            continue

        new_month = _beef_baseline_start_month(farm)
        if beef.month_start <= new_month:
            continue

        old_month = beef.month_start
        month_before_old = (old_month - dt.timedelta(days=1)).replace(day=1)
        net = _beef_net_change_between(
            db, farm=farm, month_from=new_month, month_to=month_before_old
        )
        beef.opening_count = int(beef.opening_count) - net
        beef.month_start = new_month


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
        ("GAD", STOCK_GROUP_YOUNGSTOCK, "2024-12-01", 1318),
        ("CM", STOCK_GROUP_BEEF, "2024-04-01", 217),
        ("GAD", STOCK_GROUP_BEEF, "2024-12-01", 13),
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
            if baseline and baseline.opening_count in (1780, 1781):
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
            if gad_ys and gad_ys.opening_count == 1315:
                gad_ys.opening_count = 1318
                db.commit()
            for farm, legacy_month, opening in (
                ("CM", dt.date(2025, 4, 1), 74),
                ("GAD", dt.date(2025, 4, 1), 15),
            ):
                beef = db.scalar(
                    select(StockOpeningBaseline).where(
                        StockOpeningBaseline.farm == farm,
                        StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
                    )
                )
                if beef is None:
                    db.add(
                        StockOpeningBaseline(
                            farm=farm,
                            stock_group=STOCK_GROUP_BEEF,
                            month_start=legacy_month,
                            opening_count=opening,
                        )
                    )
                    db.flush()
                elif beef.month_start == legacy_month:
                    if farm == "GAD" and beef.opening_count in (0, 13):
                        beef.opening_count = 15
                    if farm == "CM" and beef.opening_count in (0, 66):
                        beef.opening_count = 74
            _extend_beef_opening_baselines(db)
            cm_beef = db.scalar(
                select(StockOpeningBaseline).where(
                    StockOpeningBaseline.farm == "CM",
                    StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
                )
            )
            if (
                cm_beef is not None
                and cm_beef.month_start == _beef_baseline_start_month("CM")
                and cm_beef.opening_count == 191
            ):
                cm_beef.opening_count = 217
            for farm, opening in (("CM", 217), ("GAD", 13)):
                new_month = _beef_baseline_start_month(farm)
                existing = db.scalar(
                    select(StockOpeningBaseline).where(
                        StockOpeningBaseline.farm == farm,
                        StockOpeningBaseline.stock_group == STOCK_GROUP_BEEF,
                    )
                )
                if existing is None:
                    db.add(
                        StockOpeningBaseline(
                            farm=farm,
                            stock_group=STOCK_GROUP_BEEF,
                            month_start=new_month,
                            opening_count=opening,
                        )
                    )
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


def _migrate_hr_schema() -> None:
    """Ensure contract storage exists; optionally seed default DocuSeal template."""
    from pathlib import Path

    from sqlalchemy import select

    from app.config import (
        CONTRACTS_STORAGE_DIR,
        DOCUSEAL_CWRTMALLE_TEMPLATE_ID,
        DOCUSEAL_CWRTMALLE_TEMPLATE_NAME,
        DOCUSEAL_GREENACRE_TEMPLATE_ID,
        DOCUSEAL_GREENACRE_TEMPLATE_NAME,
    )
    from app.models import ContractTemplate

    Path(CONTRACTS_STORAGE_DIR, "signed").mkdir(parents=True, exist_ok=True)

    inspector = inspect(engine)

    # Add new-starter columns to the employees table if missing.
    if "employees" in inspector.get_table_names():
        existing_cols = {col["name"] for col in inspector.get_columns("employees")}
        new_columns = {
            "business": "VARCHAR(64)",
            "title": "VARCHAR(16)",
            "working_days_per_week": "FLOAT",
            "working_hours_per_day": "FLOAT",
            "driving_license_number_enc": "TEXT",
            "license_points": "VARCHAR(255)",
            "right_to_work_share_code": "VARCHAR(64)",
            "bank_name": "VARCHAR(128)",
            "account_holder_name": "VARCHAR(128)",
            "sort_code_enc": "TEXT",
            "account_number_enc": "TEXT",
            "next_of_kin_name": "VARCHAR(255)",
            "next_of_kin_relationship": "VARCHAR(64)",
            "next_of_kin_phone": "VARCHAR(64)",
        }
        missing = {k: v for k, v in new_columns.items() if k not in existing_cols}
        if missing:
            with engine.begin() as conn:
                for name, ddl_type in missing.items():
                    conn.execute(
                        text(f"ALTER TABLE employees ADD COLUMN {name} {ddl_type}")
                    )

    if "contract_templates" not in inspector.get_table_names():
        return

    # (env var, display name, description) pairs to seed by DocuSeal template id.
    seeds = [
        (
            DOCUSEAL_CWRTMALLE_TEMPLATE_ID,
            DOCUSEAL_CWRTMALLE_TEMPLATE_NAME,
            "Cwrt Malle employment contract (seeded from env)",
        ),
        (
            DOCUSEAL_GREENACRE_TEMPLATE_ID,
            DOCUSEAL_GREENACRE_TEMPLATE_NAME,
            "Green Acre Dairy employment contract (seeded from env)",
        ),
    ]

    with SessionLocal() as db:
        added = False
        for raw_id, name, description in seeds:
            if not raw_id:
                continue
            try:
                template_id = int(raw_id)
            except ValueError:
                continue
            exists = db.scalar(
                select(ContractTemplate).where(
                    ContractTemplate.docuseal_template_id == template_id
                )
            )
            if exists:
                continue
            db.add(
                ContractTemplate(
                    name=name,
                    docuseal_template_id=template_id,
                    description=description,
                    is_active=True,
                )
            )
            added = True
        if added:
            db.commit()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
