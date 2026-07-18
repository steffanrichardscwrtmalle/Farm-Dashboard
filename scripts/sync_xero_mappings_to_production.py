"""
Copy local Xero organisation + budget-heading mappings to production PostgreSQL.

Copies:
  - xero_organisations (tenant → dashboard business)
  - xero_account_budget_mappings (resolved by heading name so mapping IDs can differ)

Does not copy OAuth tokens, invoices, journals, or chart-of-accounts sync data.

Usage (PowerShell, from Farm-Dashboard-Web):

  $env:TARGET_DATABASE_URL="postgresql+psycopg://user:pass@host/dbname"
  py scripts/sync_xero_mappings_to_production.py --dry-run
  py scripts/sync_xero_mappings_to_production.py --replace --yes

Source defaults to DATABASE_URL from `.env` (local SQLite).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL, _normalize_database_url
from app.db import init_db
from app.models import (
    FinancialForecastMapping,
    XeroAccountBudgetMapping,
    XeroOrganisation,
)


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


def _heading_key(row: FinancialForecastMapping) -> tuple[str, str, str, str]:
    return (
        (row.item_type or "").strip(),
        (row.band or "").strip(),
        (row.group or "").strip(),
        (row.heading or "").strip(),
    )


def _load_heading_index(session: Session) -> dict[tuple[str, str, str, str], int]:
    return {
        _heading_key(row): int(row.id)
        for row in session.scalars(select(FinancialForecastMapping)).all()
    }


def _load_local_budget_payload(src: Session) -> list[dict]:
    headings = {
        int(row.id): _heading_key(row)
        for row in src.scalars(select(FinancialForecastMapping)).all()
    }
    payload: list[dict] = []
    for row in src.scalars(select(XeroAccountBudgetMapping)).all():
        key = headings.get(int(row.mapping_id))
        if key is None:
            print(
                f"  WARN: skip mapping tenant={row.tenant_id} account={row.account_id} "
                f"— unknown mapping_id={row.mapping_id}"
            )
            continue
        payload.append(
            {
                "tenant_id": row.tenant_id,
                "account_id": row.account_id,
                "account_code": row.account_code,
                "heading_key": key,
            }
        )
    return payload


def _load_local_orgs(src: Session) -> list[dict]:
    return [
        {
            "tenant_id": row.tenant_id,
            "tenant_name": row.tenant_name or "",
            "tenant_type": row.tenant_type,
            "dashboard_business": row.dashboard_business,
            "is_active": bool(row.is_active),
        }
        for row in src.scalars(select(XeroOrganisation)).all()
    ]


def _sync_orgs(tgt: Session, orgs: list[dict], *, replace: bool) -> tuple[int, int]:
    if replace:
        tgt.execute(delete(XeroOrganisation))
        tgt.commit()

    existing = {
        row.tenant_id: row
        for row in tgt.scalars(select(XeroOrganisation)).all()
    }
    inserted = 0
    updated = 0
    for org in orgs:
        row = existing.get(org["tenant_id"])
        if row is None:
            tgt.add(
                XeroOrganisation(
                    tenant_id=org["tenant_id"],
                    tenant_name=org["tenant_name"],
                    tenant_type=org["tenant_type"],
                    dashboard_business=org["dashboard_business"],
                    is_active=org["is_active"],
                )
            )
            inserted += 1
        else:
            row.tenant_name = org["tenant_name"] or row.tenant_name
            row.tenant_type = org["tenant_type"]
            row.dashboard_business = org["dashboard_business"]
            row.is_active = org["is_active"]
            updated += 1
    tgt.commit()
    return inserted, updated


def _sync_budget_mappings(
    tgt: Session,
    payload: list[dict],
    *,
    replace: bool,
) -> tuple[int, int]:
    heading_index = _load_heading_index(tgt)
    if replace:
        tgt.execute(delete(XeroAccountBudgetMapping))
        tgt.commit()

    existing = {
        (row.tenant_id, row.account_id): row
        for row in tgt.scalars(select(XeroAccountBudgetMapping)).all()
    }
    written = 0
    skipped = 0
    for item in payload:
        mapping_id = heading_index.get(item["heading_key"])
        if mapping_id is None:
            print(
                f"  WARN: skip {item['tenant_id']} / {item['account_code'] or item['account_id']} "
                f"— heading not on target: {item['heading_key']}"
            )
            skipped += 1
            continue
        key = (item["tenant_id"], item["account_id"])
        row = existing.get(key)
        if row is None:
            tgt.add(
                XeroAccountBudgetMapping(
                    tenant_id=item["tenant_id"],
                    account_id=item["account_id"],
                    account_code=item["account_code"],
                    mapping_id=mapping_id,
                )
            )
        else:
            row.account_code = item["account_code"]
            row.mapping_id = mapping_id
        written += 1
    tgt.commit()
    return written, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy local Xero org + budget mappings to production."
    )
    parser.add_argument(
        "--source-url",
        default="",
        help="Local DB URL (default: DATABASE_URL from .env)",
    )
    parser.add_argument(
        "--target-url",
        default=os.getenv("TARGET_DATABASE_URL", "").strip(),
        help="Production PostgreSQL URL (or set TARGET_DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace target Xero org rows and budget mappings before writing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --replace to confirm overwriting production mappings",
    )
    args = parser.parse_args()

    source_url = _normalize_database_url(args.source_url or DATABASE_URL)
    if source_url.startswith("sqlite:///") and not source_url.startswith("sqlite:////"):
        source_url = _sqlite_url(_resolve_sqlite_path(source_url))

    target_url = _normalize_database_url(args.target_url)
    if not target_url:
        print("ERROR: Set TARGET_DATABASE_URL or pass --target-url", file=sys.stderr)
        sys.exit(1)
    if target_url.startswith("sqlite"):
        print("ERROR: Target must be production PostgreSQL, not SQLite.", file=sys.stderr)
        sys.exit(1)

    if args.replace and not args.yes and not args.dry_run:
        print("ERROR: Pass --yes with --replace to confirm overwriting production mappings.")
        sys.exit(1)

    print(f"Source: {source_url.split('@')[-1] if '@' in source_url else source_url}")
    print(f"Target: {target_url.split('@')[-1] if '@' in target_url else target_url}")

    src_session, src_engine = _open_session(source_url)
    try:
        orgs = _load_local_orgs(src_session)
        mappings = _load_local_budget_payload(src_session)
        print(f"Local organisations: {len(orgs)}")
        for org in orgs:
            print(
                f"  {org['tenant_name'] or org['tenant_id']} → {org['dashboard_business'] or '(unmapped)'}"
            )
        print(f"Local budget mappings: {len(mappings)}")
        if args.dry_run:
            print("Dry run — no changes made.")
            return
        if not orgs and not mappings:
            print("Nothing to copy.")
            return

        init_db()
        tgt_session, tgt_engine = _open_session(target_url)
        try:
            before_orgs = tgt_session.scalar(select(func.count()).select_from(XeroOrganisation)) or 0
            before_maps = (
                tgt_session.scalar(select(func.count()).select_from(XeroAccountBudgetMapping))
                or 0
            )
            print(f"Target before: orgs={before_orgs}, budget_mappings={before_maps}")

            inserted, updated = _sync_orgs(tgt_session, orgs, replace=args.replace)
            print(f"Organisations: inserted={inserted}, updated={updated}")

            written, skipped = _sync_budget_mappings(
                tgt_session, mappings, replace=args.replace
            )
            print(f"Budget mappings written={written}, skipped={skipped}")

            after_orgs = tgt_session.scalar(select(func.count()).select_from(XeroOrganisation)) or 0
            after_maps = (
                tgt_session.scalar(select(func.count()).select_from(XeroAccountBudgetMapping))
                or 0
            )
            print(f"Target after: orgs={after_orgs}, budget_mappings={after_maps}")
            print("Done.")
        finally:
            tgt_session.close()
            tgt_engine.dispose()
    finally:
        src_session.close()
        src_engine.dispose()


if __name__ == "__main__":
    main()
