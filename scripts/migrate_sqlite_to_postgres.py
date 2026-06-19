"""
One-off migration: copy invoice data from local SQLite to Render PostgreSQL.

Does not copy users (production keeps its own login accounts).

Usage:
  set TARGET_DATABASE_URL=postgresql+psycopg://user:pass@host/dbname
  python scripts/migrate_sqlite_to_postgres.py --dry-run
  python scripts/migrate_sqlite_to_postgres.py --replace --yes

Get TARGET_DATABASE_URL from Render → farm-dashboard-db → Connect → External Database URL.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import _normalize_database_url
from app.models import (
    Base,
    ImportBatch,
    InvoiceLine,
    MappingOption,
    ProductMappingRule,
)

_DEFAULT_SQLITE = _PROJECT_ROOT / "data" / "wynnstay.db"

_DATA_TABLES = (
    ImportBatch,
    InvoiceLine,
    MappingOption,
    ProductMappingRule,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _open_session(url: str) -> tuple[Session, object]:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), engine


def _counts(session: Session) -> dict[str, int]:
    return {
        ImportBatch.__tablename__: session.scalar(select(func.count()).select_from(ImportBatch)) or 0,
        InvoiceLine.__tablename__: session.scalar(select(func.count()).select_from(InvoiceLine)) or 0,
        MappingOption.__tablename__: session.scalar(select(func.count()).select_from(MappingOption)) or 0,
        ProductMappingRule.__tablename__: session.scalar(select(func.count()).select_from(ProductMappingRule)) or 0,
    }


def _clear_target_data(session: Session) -> None:
    session.execute(delete(InvoiceLine))
    session.execute(delete(ImportBatch))
    session.execute(delete(ProductMappingRule))
    session.execute(delete(MappingOption))
    session.commit()


def _copy_rows(src: Session, tgt: Session, model: type, *, batch_size: int = 500) -> int:
    rows = list(src.scalars(select(model).order_by(model.id)).all())
    if not rows:
        return 0

    columns = [col.name for col in model.__table__.columns]
    batch: list[dict] = []
    for row in rows:
        batch.append({name: getattr(row, name) for name in columns})
        if len(batch) >= batch_size:
            tgt.bulk_insert_mappings(model, batch)
            tgt.commit()
            batch.clear()

    if batch:
        tgt.bulk_insert_mappings(model, batch)
        tgt.commit()
    return len(rows)


def _reset_postgres_sequences(engine, models: tuple[type, ...]) -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for model in models:
            table = model.__tablename__
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy invoice data from local SQLite to Render PostgreSQL."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=_DEFAULT_SQLITE,
        help=f"SQLite file to copy from (default: {_DEFAULT_SQLITE})",
    )
    parser.add_argument(
        "--target-url",
        default=os.getenv("TARGET_DATABASE_URL", "").strip(),
        help="PostgreSQL URL (or set TARGET_DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show row counts only; do not write to the target database",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing invoice/mapping data on the target before copying",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt when using --replace",
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"Source database not found: {args.source}")
        sys.exit(1)

    target_url = _normalize_database_url(args.target_url) if args.target_url else ""
    if not args.dry_run and not target_url:
        print("Set TARGET_DATABASE_URL or pass --target-url with your Render PostgreSQL URL.")
        sys.exit(1)
    if not args.dry_run and target_url.startswith("sqlite"):
        print("Target must be PostgreSQL (Render DATABASE_URL), not SQLite.")
        sys.exit(1)

    src_session, src_engine = _open_session(_sqlite_url(args.source))
    try:
        source_counts = _counts(src_session)
        print("Source:", args.source)
        for name, count in source_counts.items():
            print(f"  {name}: {count}")

        if args.dry_run:
            if target_url:
                tgt_session, tgt_engine = _open_session(target_url)
                try:
                    Base.metadata.create_all(bind=tgt_engine)
                    target_counts = _counts(tgt_session)
                    print("\nTarget (current):")
                    for name, count in target_counts.items():
                        print(f"  {name}: {count}")
                finally:
                    tgt_session.close()
                    tgt_engine.dispose()
            else:
                print("\nDry run only (no TARGET_DATABASE_URL set).")
            return

        tgt_session, tgt_engine = _open_session(target_url)
        try:
            Base.metadata.create_all(bind=tgt_engine)
            target_counts = _counts(tgt_session)
            print("\nTarget (before):")
            for name, count in target_counts.items():
                print(f"  {name}: {count}")

            if args.replace:
                if not args.yes:
                    answer = input(
                        "\nThis will DELETE all invoice/mapping data on the target. Type 'yes' to continue: "
                    ).strip()
                    if answer.lower() != "yes":
                        print("Aborted.")
                        return
                print("\nClearing target invoice/mapping tables...")
                _clear_target_data(tgt_session)

            print("\nCopying data...")
            copied: dict[str, int] = {}
            for model in _DATA_TABLES:
                n = _copy_rows(src_session, tgt_session, model)
                copied[model.__tablename__] = n
                print(f"  {model.__tablename__}: {n}")

            _reset_postgres_sequences(tgt_engine, _DATA_TABLES)

            final_counts = _counts(tgt_session)
            print("\nTarget (after):")
            for name, count in final_counts.items():
                print(f"  {name}: {count}")
            print("\nDone. Users on production were not changed.")
        finally:
            tgt_session.close()
            tgt_engine.dispose()
    finally:
        src_session.close()
        src_engine.dispose()


if __name__ == "__main__":
    main()
