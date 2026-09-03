"""CLI entrypoint for the 08:00 morning brief.

Short Telegram message to the owner, built from three deterministic,
read-only sources:

- unresolved ``- [ ] ...`` checkboxes from yesterday's daily note
  (``vault/daily/YYYY-MM-DD.md``);
- this week's ONE Big Thing from ``vault/goals/3-weekly.md``;
- the count of open compiled-page conflicts, via
  ``decisions_queue.list_queue_items`` -- the same function the decisions
  queue screen and the weekly review already use to derive conflict rows
  from ``compiled/**``, so this brief can never disagree with them about
  what is open.

Todoist overdue tasks are deliberately NOT fetched here. Every existing
Todoist reader in this codebase (``services/todoist_projects.py``) only
lists *projects*; real task queries (``find-tasks-by-date`` and friends)
exist only inside LLM-driven skill prompts that shell out to ``mcp-cli``
(see ``skills/todoist-ai``), not as a plain Python client. Building a new
direct client for this one CLI would be exactly the kind of speculative
addition this fix should avoid. ``build_morning_brief`` keeps a
``todoist_overdue`` parameter so a future caller with real overdue data can
still pass it in and have it rendered (capped at three); ``main`` below
never populates it, so the Todoist line is simply omitted today.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from d_brain.config import get_settings
from d_brain.manifest import ManifestValidationError
from d_brain.services.decisions_queue import CONFLICT_KIND, list_queue_items
from d_brain.services.telegram_delivery import send_telegram_text_sync

logger = logging.getLogger(__name__)

MAX_CHECKBOXES = 5
MAX_TODOIST_OVERDUE = 3

_CHECKBOX_RE = re.compile(r"(?m)^\s*-\s+\[ \]\s+(.*\S)\s*$")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_ONE_BIG_THING_HEADING_RE = re.compile(
    r"(?im)^##\s*(?:one\s+big\s+thing|главное)\s*$"
)
_NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}\s|^---\s*$")
_PLACEHOLDER_STRIP_RE = re.compile(r"[-.…>*_()\[\]\s]")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _open_checkboxes(vault_path: Path, day: date) -> list[str]:
    """Unresolved ``- [ ] ...`` items from one daily file, oldest to newest.

    Fenced code blocks are stripped first so an example ``- [ ] ...`` line
    quoted inside a ``` ``` block is not counted as a real task.
    """
    daily_path = vault_path / "daily" / f"{day.isoformat()}.md"
    if not daily_path.exists():
        return []
    content = daily_path.read_text(encoding="utf-8")
    content = _CODE_FENCE_RE.sub("", content)
    return [match.group(1).strip() for match in _CHECKBOX_RE.finditer(content)]


def _is_placeholder_text(text: str) -> bool:
    """True when *text* has no letters or digits once markup noise is removed.

    Catches unfilled template placeholders such as ``…``, ``...`` or
    ``_( )_`` that survive marker-stripping but carry no real content.
    """
    return not any(char.isalnum() for char in _PLACEHOLDER_STRIP_RE.sub("", text))


def _extract_one_big_thing(content: str) -> str | None:
    """Best-effort extract of the weekly ONE Big Thing text.

    Two section shapes exist in this project and both are handled the same
    way ``CliProcessor._extract_next_week_focus`` handles "Next Week Focus":
    strip blockquote/list markers and join what is left.

    - The vault template's plain bullet: ``## One big thing`` followed by a
      ``- ...`` list item (see
      ``resources/vault_template/goals/3-weekly.md``).
    - The processor's promoted blockquote: ``## ONE Big Thing`` or its
      Russian counterpart ``## Главное``, followed by a bold label line and
      a ``> ...`` quoted sentence (see
      ``CliProcessor._promote_next_week_focus``).

    Only a contiguous run of marker lines (``>`` for the quote form, ``-``
    for the list form) is collected. This stops at the first non-marker,
    non-blank line, which keeps the trailing
    ``<!-- This is read by the bot during daily processing -->`` HTML
    comment ``_promote_next_week_focus`` leaves behind out of the result.
    """
    heading_match = _ONE_BIG_THING_HEADING_RE.search(content)
    if heading_match is None:
        return None
    rest = content[heading_match.end() :]
    next_heading = _NEXT_HEADING_RE.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest

    marker: str | None = None
    lines: list[str] = []
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if marker is None:
            if stripped[0] not in ">-":
                break
            marker = stripped[0]
        elif stripped[0] != marker:
            break

        line = stripped.lstrip(marker).strip()
        if not line or (line.startswith("**") and line.endswith(":**")):
            continue
        if _is_placeholder_text(line):
            continue
        lines.append(line)
    return " ".join(lines).strip() or None


def _weekly_one_big_thing(vault_path: Path) -> str | None:
    weekly_path = vault_path / "goals" / "3-weekly.md"
    if not weekly_path.exists():
        return None
    return _extract_one_big_thing(weekly_path.read_text(encoding="utf-8"))


def _open_conflict_pages(vault_path: Path) -> list[str]:
    """Distinct compiled pages with an unresolved "Open Conflicts" row.

    Best-effort: ``list_queue_items`` builds a ``CompiledBriefingService``,
    which loads ``vault-manifest.json`` eagerly and raises when it is
    missing or invalid. A vault without that manifest yet (or with a
    temporarily broken one) should not take down the rest of the brief.
    ``OSError`` is caught for the same reason: the compiled queue worker
    archives pages concurrently, so a page listed by ``glob`` may be gone by
    the time ``list_queue_items`` reads it.
    """
    try:
        items = list_queue_items(vault_path)
    except (ManifestValidationError, OSError) as exc:
        logger.warning("Skipping open-conflicts block: %s", exc)
        return []

    pages: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.kind != CONFLICT_KIND or item.page in seen:
            continue
        seen.add(item.page)
        pages.append(item.page)
    return pages


def build_morning_brief(
    vault_path: Path,
    today: date,
    *,
    todoist_overdue: list[str] | None = None,
) -> str:
    """Assemble the short 08:00 owner digest.

    Pure and read-only: no writes, no model calls, no Telegram send. Each
    block is left out entirely when it has nothing to show; when every
    block is empty the whole message is one short line.
    """
    vault_path = Path(vault_path)
    yesterday = today - timedelta(days=1)

    checkboxes = _open_checkboxes(vault_path, yesterday)[:MAX_CHECKBOXES]
    one_big_thing = _weekly_one_big_thing(vault_path)
    overdue = [
        str(item).strip() for item in (todoist_overdue or []) if str(item).strip()
    ][:MAX_TODOIST_OVERDUE]
    conflict_pages = _open_conflict_pages(vault_path)

    if not checkboxes and not one_big_thing and not overdue and not conflict_pages:
        return "Сегодня без хвостов"

    lines = [f"**☀️ Утренний бриф — {today.isoformat()}**"]

    if checkboxes:
        lines.extend(["", f"**Незакрытые дела за {yesterday.isoformat()}**"])
        lines.extend(f"- {item}" for item in checkboxes)

    if one_big_thing:
        lines.extend(["", "**Главное на неделе**", one_big_thing])

    if overdue:
        lines.extend(["", "**Просроченные задачи Todoist**"])
        lines.extend(f"- {item}" for item in overdue)

    if conflict_pages:
        lines.extend(
            ["", f"**Страниц с открытыми конфликтами:** {len(conflict_pages)}"]
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the 08:00 morning brief")
    parser.add_argument(
        "--date",
        dest="day",
        help="Date to build the brief for (defaults to today), YYYY-MM-DD",
    )
    args = parser.parse_args()

    today = _parse_date(args.day) or date.today()
    settings = get_settings()

    try:
        brief = build_morning_brief(settings.vault_path, today)
    except Exception as exc:  # pragma: no cover - CLI boundary
        logger.exception("Morning brief build crashed")
        sys.stderr.write(f"Morning brief crashed: {exc}\n")
        try:
            send_telegram_text_sync(f"⚠️ Утренний бриф не построился: {exc}")
        except Exception as notify_exc:  # pragma: no cover - notification boundary
            logger.warning(
                "Failed to notify about morning brief failure: %s", notify_exc
            )
        return 1

    sys.stdout.write(brief)
    if not brief.endswith("\n"):
        sys.stdout.write("\n")

    try:
        send_telegram_text_sync(brief)
    except Exception as exc:  # pragma: no cover - notification boundary
        logger.warning("Failed to send morning brief: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
