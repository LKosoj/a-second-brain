"""CLI entrypoint for on-demand owner briefs from compiled/ (ТЗ 7.6).

Builds one brief via ``compiled_briefs.build_brief`` (a pure function -- see
that module's docstring), then writes it to
``summaries/briefs/YYYY-MM-DD-<type>-<slug>.md``, touches the source page
(ТЗ 6.2: a page reaching a brief counts as "used"), and sends the brief to
the owner over Telegram. ``--dry-run`` only prints the brief to stdout: no
write, no touch, no Telegram send. A query that matches nothing prints a
clear "not-found" line and exits 0 -- ТЗ 7.6 step 1 explicitly calls that "a
clear result, not a file or an exception", the same treatment
``run_compiled_digest.py``/``compiled_enrich_report.py`` give a no-work day.

The "Собрать бриф" ``/menu`` button is already wired to this same
build/write/touch path (``bot/handlers/brief.py``'s ``process_brief_request``
mirrors this CLI's ``main()`` -- see that module's docstring); a
control-plane registry entry for on-demand brief requests is still
outstanding.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from d_brain.config import get_settings
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefs import BRIEF_TYPES, build_brief
from d_brain.services.compiled_briefs import brief_path as _brief_path
from d_brain.services.compiled_briefs import render_brief_note as _render_note
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.qmd import QmdService
from d_brain.services.telegram_delivery import send_telegram_text_sync
from d_brain.services.vault_lock import vault_write_lock

logger = logging.getLogger(__name__)

# ``_render_note``/``_brief_path`` used to be defined here; задача L moved
# their bodies to ``compiled_briefs.render_brief_note``/``.brief_path`` (also
# used by the bot's "Собрать бриф" button) and they are re-imported above
# under their old private names so this module's behavior and its existing
# tests (which call these names directly) are unchanged.


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and deliver one owner brief from compiled/"
    )
    parser.add_argument("--type", choices=BRIEF_TYPES, required=True)
    parser.add_argument(
        "--query", required=True, help="Target page slug, vault path, or free text"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the brief to stdout without writing or sending it",
    )
    args = parser.parse_args()

    settings = get_settings()
    vault_path = Path(settings.vault_path)

    result = build_brief(vault_path, brief_type=args.type, query=args.query)
    if result is None:
        sys.stdout.write(f"not-found: no {args.type} page matches {args.query!r}\n")
        return 0

    if args.dry_run:
        sys.stdout.write(result.markdown + "\n")
        return 0

    today = date.today()
    manifest = load_manifest_for_vault(vault_path)
    # Path selection must happen under the vault write lock: computing the
    # counter-suffixed path before taking the lock let two near-simultaneous
    # CLI runs both see the same suffix as free and then both write it, the
    # second silently overwriting the first. Under the lock, a second run
    # only computes its path after the first run's file already exists on
    # disk.
    with vault_write_lock(vault_path) as lock:
        path = _brief_path(vault_path, result, today)
        write_validated_vault_markdown(
            vault_path,
            path,
            _render_note(result, today),
            manifest=manifest,
            existing_lock=lock,
        )

    # ТЗ 6.2: a page that reaches a brief counts as "used" and gets the
    # existing step-wise touch promotion -- reusing the same memory-engine
    # path ``run_qmd.py``'s ``qmd get`` already calls
    # (``QmdService.touch_notes``), not a new subprocess launch of our own.
    # Best-effort: a touch failure must not take down the brief itself.
    try:
        QmdService(vault_path).touch_notes([result.source_rel_path])
    except Exception as exc:  # pragma: no cover - best-effort touch
        logger.warning(
            "Failed to touch brief source %s: %s", result.source_rel_path, exc
        )

    try:
        send_telegram_text_sync(result.markdown, rich=True)
    except Exception as exc:  # pragma: no cover - notification boundary
        logger.warning("Failed to send brief: %s", exc)

    sys.stdout.write(path.relative_to(vault_path).as_posix() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
