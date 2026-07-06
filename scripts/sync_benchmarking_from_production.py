"""
One-off: copy Benchmarking ration data from production PostgreSQL to local SQLite.

Copies: ration_ingredients, ration_ingredient_costs, farm_rations,
farm_ration_ingredients, farm_ration_inclusions.

Does not copy users or other app data.

Usage:
  1. Get the External Database URL from Render → farm-dashboard-db → Connect.
  2. In PowerShell (from Farm-Dashboard-Web):

     $env:SOURCE_DATABASE_URL="postgresql+psycopg://user:pass@host/dbname"
     py scripts/sync_benchmarking_from_production.py --dry-run
     py scripts/sync_benchmarking_from_production.py --replace --yes

  Local target is DATABASE_URL from your `.env` (default sqlite:///data/wynnstay.db).
  Stop the local dev server before running so the SQLite file is not locked.
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

from app.config import DATABASE_URL, _normalize_database_url
from app.db import init_db
from app.models import (
    FarmRation,
    FarmRationInclusion,
    FarmRationIngredient,
    RationIngredient,
    RationIngredientCost,
)

_DEFAULT_SQLITE = _PROJECT_ROOT / "data" / "wynnstay.db"

# Insert order (parents before children).
_COPY_MODELS: tuple[type, ...] = (
    RationIngredient,
    RationIngredientCost,
    FarmRation,
    FarmRationIngredient,
    FarmRationInclusion,
)

# Delete order (children before parents).
_DELETE_MODELS: tuple[type, ...] = tuple(reversed(_COPY_MODELS))


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _resolve_sqlite_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"Expected sqlite URL, got: {url}")
    raw = url.replace("sqlite:///", "", 1)
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


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
        model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
        for model in _COPY_MODELS
    }


def _clear_target(session: Session) -> None:
    for model in _DELETE_MODELS:
        session.execute(delete(model))
    session.commit()


def _copy_rows(src: Session, tgt: Session, model: type) -> int:
    rows = list(src.scalars(select(model).order_by(model.id)).all())
    if not rows:
        return 0
    columns = [col.name for col in model.__table__.columns]
    payload = [{name: getattr(row, name) for name in columns} for row in rows]
    tgt.bulk_insert_mappings(model, payload)
    tgt.commit()
    return len(rows)


def _reset_sqlite_sequences(engine, models: tuple[type, ...]) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        for model in models:
            table = model.__tablename__
            max_id = conn.execute(
                text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            ).scalar() or 0
            conn.execute(
                text("DELETE FROM sqlite_sequence WHERE name = :name"),
                {"name": table},
            )
            if max_id:
                conn.execute(
                    text(
                        "INSERT INTO sqlite_sequence (name, seq) VALUES (:name, :seq)"
                    ),
                    {"name": table, "seq": int(max_id)},
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy Benchmarking ration tables from production into local SQLite."
    )
    parser.add_argument(
        "--source-url",
        default=os.getenv("SOURCE_DATABASE_URL", "").strip(),
        help="Production PostgreSQL URL (or set SOURCE_DATABASE_URL)",
    )
    parser.add_argument(
        "--target-url",
        default="",
        help="Target DB URL (default: DATABASE_URL from .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show row counts only; do not write",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing ration data in the target before copying",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --replace to confirm destructive target update",
    )
    args = parser.parse_args()

    source_url = _normalize_database_url(args.source_url)
    if not source_url:
        print("ERROR: Set SOURCE_DATABASE_URL or pass --source-url", file=sys.stderr)
        sys.exit(1)

    target_url = _normalize_database_url(args.target_url or DATABASE_URL)
    if target_url.startswith("sqlite:///") and not target_url.startswith("sqlite:////"):
        target_url = _sqlite_url(_resolve_sqlite_path(target_url))

    if "render.com" in target_url or target_url.startswith("postgresql"):
        print(
            "ERROR: Target looks like production. Point DATABASE_URL at local SQLite.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.replace and not args.yes and not args.dry_run:
        print("ERROR: Pass --yes with --replace to confirm overwriting local ration data.")
        sys.exit(1)

    print(f"Source: {source_url.split('@')[-1] if '@' in source_url else source_url}")
    print(f"Target: {target_url}")

    src_session, src_engine = _open_session(source_url)
    try:
        src_counts = _counts(src_session)
        print("Source row counts:")
        for table, count in src_counts.items():
            print(f"  {table}: {count}")
        if args.dry_run:
            print("Dry run — no changes made.")
            return
        if sum(src_counts.values()) == 0:
            print("Nothing to copy.")
            return

        init_db()
        tgt_session, tgt_engine = _open_session(target_url)
        try:
            if args.replace:
                print("Clearing local ration tables…")
                _clear_target(tgt_session)
            print("Copying rows…")
            copied: dict[str, int] = {}
            for model in _COPY_MODELS:
                n = _copy_rows(src_session, tgt_session, model)
                copied[model.__tablename__] = n
                print(f"  {model.__tablename__}: {n}")
            _reset_sqlite_sequences(tgt_engine, _COPY_MODELS)
            print("Done. Local counts:")
            for table, count in _counts(tgt_session).items():
                print(f"  {table}: {count}")
        finally:
            tgt_session.close()
            tgt_engine.dispose()
    finally:
        src_session.close()
        src_engine.dispose()


if __name__ == "__main__":
    main()
