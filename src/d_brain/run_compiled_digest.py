"""CLI entrypoint for the daily compiled-enrichment digest (ТЗ 7.1).

Builds the digest via ``compiled_enrich_report.build_daily_digest`` (a pure
function -- see that module's docstring), then writes it to
``summaries/compile/YYYY-MM-DD.md`` and sends it to the owner over Telegram.
``--dry-run`` only prints the digest to stdout: no write, no Telegram send.

``pass_status`` comes from the real ТЗ 5.2 step 6 pass journal
(``.session/compile-enrich.json``, written by ``compiled_briefings.py``'s
``_write_pass_journal``) via ``compiled_enrich_report.read_pass_status`` --
the same reader the bot's "Дайджест" button (``bot/handlers/menu.py``) and
the ``maintenance.compiled-digest`` nightly cycle
(``processor.py``'s ``_run_compiled_digest_cycle``, registered in
``control_plane/registry.py``) use, so the "no-work" suppression in
``build_daily_digest`` fires here too.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from d_brain.config import get_settings
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_enrich_report import build_daily_digest, read_pass_status
from d_brain.services.compiled_enrich_report import digest_path as _digest_path
from d_brain.services.compiled_enrich_report import render_digest_note as _render_note
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.telegram_delivery import send_telegram_text_sync
from d_brain.services.vault_lock import vault_write_lock

logger = logging.getLogger(__name__)

# ``_render_note``/the digest path expression used to live here; задача L
# moved them to ``compiled_enrich_report.render_digest_note``/``digest_path``
# (also used by the bot's "Дайджест" button) and ``_render_note`` is
# re-imported above under its old private name so this module's behavior and
# its existing tests (which call that name directly) are unchanged.


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and deliver the compiled-enrichment digest"
    )
    parser.add_argument("--date", dest="day", help="Digest date, YYYY-MM-DD")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to stdout without writing or sending it",
    )
    args = parser.parse_args()

    day = _parse_date(args.day)
    settings = get_settings()
    vault_path = Path(settings.vault_path)

    digest = build_daily_digest(
        vault_path, day, pass_status=read_pass_status(vault_path)
    )
    if digest is None:
        sys.stdout.write(f"no-work: nothing to report for {day.isoformat()}\n")
        return 0

    if args.dry_run:
        sys.stdout.write(digest + "\n")
        return 0

    manifest = load_manifest_for_vault(vault_path)
    path = _digest_path(vault_path, day)
    with vault_write_lock(vault_path) as lock:
        write_validated_vault_markdown(
            vault_path,
            path,
            _render_note(day, digest),
            manifest=manifest,
            existing_lock=lock,
        )

    try:
        send_telegram_text_sync(digest, rich=True)
    except Exception as exc:  # pragma: no cover - notification boundary
        logger.warning("Failed to send compiled digest: %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
