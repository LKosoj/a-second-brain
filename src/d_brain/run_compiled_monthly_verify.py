"""CLI entrypoint for the monthly compiled fact-check (ТЗ 6.6).

Runs ``compiled_fact_check.run_monthly_fact_check`` against the configured
vault: a bounded, deterministic (zero model calls) re-check of the oldest
core/active/warm compiled pages, patching only ``last_verified``/
``confidence`` and queuing pages that fail the check for the owner instead
of archiving them automatically.
"""

from __future__ import annotations

import argparse
import json
import sys

from d_brain.config import get_settings
from d_brain.services.compiled_fact_check import (
    DEFAULT_FACT_CHECK_PAGE_LIMIT,
    run_monthly_fact_check,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run compiled monthly fact-check")
    parser.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_FACT_CHECK_PAGE_LIMIT,
        help="Maximum stale pages to re-check in one run",
    )
    args = parser.parse_args()

    settings = get_settings()
    result = run_monthly_fact_check(settings.vault_path, page_limit=args.page_limit)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
