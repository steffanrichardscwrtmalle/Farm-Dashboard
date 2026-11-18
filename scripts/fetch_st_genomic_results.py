"""Download ST Strategy GBR female genomics into the OneDrive Genomic Results folder.

Designed for a Friday Render cron:

    python scripts/fetch_st_genomic_results.py

Requires ``ST_STRATEGY_EMAIL``, ``ST_STRATEGY_PASSWORD``, and the same Graph
OneDrive settings as the genomic import. The newest workbook in that folder is
what ``scripts/import_genomic_results.py`` loads.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.graph_onedrive import graph_is_configured
from app.services.st_genomics import StGenomicsError, store_gbr_females_workbook


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _configure_stdio()
    if not graph_is_configured():
        print(
            "OneDrive is not configured. Set Graph API variables or LOCAL_HERD_EXPORT_DIR.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    try:
        print("Step: downloading ST GBR female genomic workbook…", flush=True)
        result = store_gbr_females_workbook()
        print(
            f"Saved {result['relative_path']} ({result['bytes']:,} bytes)",
            flush=True,
        )
        return 0
    except StGenomicsError as exc:
        print(f"ST genomic fetch failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - cron entry point: log and exit non-zero
        print(
            f"ST genomic fetch failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
