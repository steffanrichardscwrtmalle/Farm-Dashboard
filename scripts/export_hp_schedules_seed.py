"""Export active HP schedules from local DB to a JSON seed file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db import SessionLocal, init_db
from app.services.hp_schedules import list_hp_schedules

# Keep outside gitignored /data/ so production can seed from the repo.
SEED_PATH = _PROJECT_ROOT / "app" / "seed_data" / "hp_schedules.json"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        rows = list_hp_schedules(db)
    finally:
        db.close()

    payload = [
        {
            "business": row["business"],
            "name": row["name"],
            "description": row["description"],
            "monthly_capital": row["monthly_capital"],
            "monthly_interest": row["monthly_interest"],
            "months": row["months"],
            "payment_day": row["payment_day"],
            "start_month": row["start_month"],
        }
        for row in rows
    ]
    SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEED_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload)} schedules to {SEED_PATH}")


if __name__ == "__main__":
    main()
