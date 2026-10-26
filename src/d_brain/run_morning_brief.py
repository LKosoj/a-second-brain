"""CLI entrypoint for the 08:00 morning brief.

Short Telegram message to the owner, built from deterministic, read-only
sources (the only network calls are the optional Todoist and calendar blocks):

- unresolved ``- [ ] ...`` checkboxes from yesterday's daily note
  (``vault/daily/YYYY-MM-DD.md``);
- this week's ONE Big Thing from ``vault/goals/3-weekly.md``;
- the count of open compiled-page conflicts, via
  ``decisions_queue.list_queue_items`` -- the same function the decisions
  queue screen and the weekly review already use to derive conflict rows
  from ``compiled/**``, so this brief can never disagree with them about
  what is open;
- overdue Todoist tasks, via ``TodoistTasks.today()`` -- the same client and
  the same notion of "overdue" as the bot's task screen. Best-effort: with
  no API key or on any Todoist failure the block is omitted and the reason
  is logged. ``main`` fills ``build_morning_brief(todoist_overdue=...)``;
- today's meetings from the optional ICS calendar (``CALENDAR_ICS_URL``),
  best-effort like Todoist; a broken link shows one "calendar unavailable"
  line so a missing block is not mistaken for "no meetings";
- on Mondays, up to two short "on this day" snippets from daily notes of
  the same day number in earlier months (a year ago first).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from d_brain.clock import owner_today, owner_tz
from d_brain.config import get_settings
from d_brain.manifest import ManifestValidationError
from d_brain.services import calendar_ics
from d_brain.services.decisions_queue import CONFLICT_KIND, list_queue_items
from d_brain.services.telegram_delivery import send_telegram_text_sync
from d_brain.services.todoist_tasks import TodoistTasks

logger = logging.getLogger(__name__)

MAX_CHECKBOXES = 5
MAX_TODOIST_OVERDUE = 3
MAX_ON_THIS_DAY = 2
MAX_ON_THIS_DAY_CHARS = 140
MAX_LOOKBACK_MONTHS = 60

_CHECKBOX_RE = re.compile(r"(?m)^\s*-\s+\[ \]\s+(.*\S)\s*$")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_ONE_BIG_THING_HEADING_RE = re.compile(
    r"(?im)^##\s*(?:one\s+big\s+thing|главное)\s*$"
)
_NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}\s|^---\s*$")
_ENTRY_HEADING_RE = re.compile(r"(?m)^## \d{2}:\d{2} \[")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EMBED_LINE_RE = re.compile(r"(?m)^\s*!\[\[.*$")
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


def _todoist_overdue(settings: Any) -> list[str]:
    """Overdue Todoist tasks as ``"title (YYYY-MM-DD)"``, most urgent first.

    Best-effort: ``TodoistTasks`` shells out to ``mcp-cli`` (subprocess,
    network, JSON parsing), so many different errors are possible, and none
    of them should take down the rest of the brief.
    """
    if not settings.todoist_api_key.strip():
        logger.info("Skipping Todoist overdue block: no API key")
        return []
    try:
        timezone, tasks = TodoistTasks(
            settings.vault_path, settings.todoist_api_key
        ).today()
        now = datetime.now(ZoneInfo(timezone))
        overdue = sorted(
            (task for task in tasks if task.overdue(now)),
            key=lambda task: (task.priority, task.due, task.id),
        )
    except Exception as exc:
        logger.warning("Skipping Todoist overdue block: %s", exc)
        return []
    return [f"{task.content} ({task.due[:10]})" for task in overdue]


def _calendar_meetings(settings: Any, day: date) -> tuple[list[str], bool]:
    """Meeting lines for ``day`` and whether the calendar failed.

    Best-effort: the feed is optional and lives on someone else's server.
    """
    url = str(getattr(settings, "calendar_ics_url", "")).strip()
    if not url:
        return [], False
    result = calendar_ics.fetch_day(url, day)
    if result.error:
        return [], True
    return calendar_ics.format_brief_lines(result.events, day, owner_tz()), False


def _same_day_earlier(today: date) -> list[date]:
    """Same day number in earlier months: anniversaries first, then nearest.

    Months where the day does not exist (31st, 29 Feb) are skipped.
    """
    found: list[tuple[int, date]] = []
    for back in range(1, MAX_LOOKBACK_MONTHS + 1):
        year, month = divmod(today.year * 12 + today.month - 1 - back, 12)
        try:
            found.append((back, date(year, month + 1, today.day)))
        except ValueError:
            continue
    found.sort(key=lambda item: (item[0] % 12 != 0, item[0]))
    return [day for _back, day in found]


_MARKUP_STRIP = str.maketrans("", "", "*_`[]")


def _first_entry_snippet(content: str) -> str | None:
    """First meaningful text line of a daily note, skipping photo-only entries."""
    for block in _ENTRY_HEADING_RE.split(content)[1:]:
        block = _EMBED_LINE_RE.sub("", _HTML_COMMENT_RE.sub("", block))
        # The first line is the rest of the heading ("text]"), not content.
        for line in block.splitlines()[1:]:
            text = line.strip()
            if text and not _is_placeholder_text(text):
                # Daily markup would otherwise reformat the Telegram message.
                text = text.translate(_MARKUP_STRIP)
                if len(text) > MAX_ON_THIS_DAY_CHARS:
                    return text[: MAX_ON_THIS_DAY_CHARS - 1].rstrip() + "…"
                return text
    return None


def _on_this_day(vault_path: Path, today: date) -> list[tuple[date, str]]:
    """Snippets from earlier months' daily notes, on Mondays only.

    A pure function of ``today`` (no "last shown" state), so a fixed weekday
    keeps the block weekly without any extra setting.
    """
    if today.weekday() != 0:
        return []
    found: list[tuple[date, str]] = []
    for day in _same_day_earlier(today):
        path = vault_path / "daily" / f"{day.isoformat()}.md"
        try:
            snippet = _first_entry_snippet(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            # One broken old note must not take the whole brief down.
            logger.warning("Skipping on-this-day note %s: %s", path.name, exc)
            continue
        if snippet:
            found.append((day, snippet))
            if len(found) == MAX_ON_THIS_DAY:
                break
    return found


def build_morning_brief(
    vault_path: Path,
    today: date,
    *,
    todoist_overdue: list[str] | None = None,
    meetings: list[str] | None = None,
    calendar_error: bool = False,
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
    meeting_lines = [line for line in (meetings or []) if line.strip()]

    # An unreadable calendar is shown, so it counts as something to show.
    if not (
        checkboxes
        or one_big_thing
        or overdue
        or conflict_pages
        or meeting_lines
        or calendar_error
    ):
        return "Сегодня без хвостов"

    lines = [f"**☀️ Утренний бриф — {today.isoformat()}**"]

    if checkboxes:
        lines.extend(["", f"**Незакрытые дела за {yesterday.isoformat()}**"])
        lines.extend(f"- {item}" for item in checkboxes)

    if one_big_thing:
        lines.extend(["", "**Главное на неделе**", one_big_thing])

    if meeting_lines:
        lines.extend(["", "**Встречи сегодня**", *meeting_lines])
    elif calendar_error:
        lines.extend(["", "**Встречи сегодня:** календарь недоступен"])

    if overdue:
        lines.extend(["", "**Просроченные задачи Todoist**"])
        lines.extend(f"- {item}" for item in overdue)

    if conflict_pages:
        lines.extend(
            ["", f"**Страниц с открытыми конфликтами:** {len(conflict_pages)}"]
        )

    # Not part of the "no tails" check above: a memory is not a leftover.
    memories = _on_this_day(vault_path, today)
    if memories:
        lines.extend(["", "**Что было в этот день**"])
        lines.extend(
            f"- {day.isoformat()}: {snippet} [[daily/{day.isoformat()}]]"
            for day, snippet in memories
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

    # Settings first: loading them fixes the owner's timezone for "today".
    settings = get_settings()
    today = _parse_date(args.day) or owner_today()

    # Overdue tasks are always as of now; --date only shifts "yesterday".
    overdue = _todoist_overdue(settings)
    # Meetings follow --date, so a re-run reproduces that day.
    meetings, calendar_error = _calendar_meetings(settings, today)

    try:
        brief = build_morning_brief(
            settings.vault_path,
            today,
            todoist_overdue=overdue,
            meetings=meetings,
            calendar_error=calendar_error,
        )
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
