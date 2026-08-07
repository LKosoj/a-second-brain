"""Runtime-owned status markers for daily entries."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

ENTRY_STATUS_ALREADY_PROCESSED = "already_processed"
# Anchored to one single line, like every other reader of the runtime's own
# markup: format_entry_status_comments always writes one comment per line,
# while a forged one arrives inside someone else's forwarded text, where
# storage.append_to_daily has indented it by a space
# (escape_embedded_runtime_markup). Without the anchor that one space would
# not defuse anything, since this pattern is used with findall. The inner
# gaps are "[ \t]", not "\s": "\s" spans newlines, so a marker split across
# two lines ("<!--\nd-brain:entry-status: ... -->") still matched here while
# neither of its lines matched the line-by-line escaping. The trailing
# "\r?" keeps CRLF daily files readable, since MULTILINE "$" stops before
# the "\n" but not before the "\r".
ENTRY_STATUS_RE = re.compile(
    r"^<!--[ \t]*d-brain:entry-status:[ \t]*([a-z][a-z0-9_-]*)[ \t]*-->[ \t]*\r?$",
    re.MULTILINE,
)
DAILY_ENTRY_RE = re.compile(
    r"^##\s+(?P<time>\d{2}:\d{2})\s+(?P<type>\[[^\]]+\])\s*$",
    re.MULTILINE,
)
ENTRY_STATUS_VALUE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class DailyEntryStatus:
    """Runtime metadata extracted from one raw daily entry block."""

    time: str
    entry_type: str
    statuses: tuple[str, ...]


def normalize_entry_type(value: str) -> str:
    """Normalize one entry type marker for matching."""

    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return stripped


def normalize_entry_statuses(statuses: Iterable[str]) -> list[str]:
    """Normalize, validate, and deduplicate entry statuses while preserving order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_status in statuses:
        status = str(raw_status or "").strip().lower()
        if not status or not ENTRY_STATUS_VALUE_RE.fullmatch(status):
            continue
        if status in seen:
            continue
        normalized.append(status)
        seen.add(status)
    return normalized


def format_entry_status_comments(statuses: Iterable[str]) -> str:
    """Render entry statuses as runtime-owned HTML comments."""

    normalized = normalize_entry_statuses(statuses)
    return "\n".join(
        f"<!-- d-brain:entry-status: {status} -->" for status in normalized
    )


def _normalize_newlines(text: str) -> str:
    """Make every line ending a "\\n", the only one MULTILINE "^" reacts to.

    Both patterns here are anchored, and Python anchors them on "\\n" alone --
    a daily file written with bare "\\r" line endings (frontmatter._detect_newline
    keeps that variant alive) would otherwise read as one single line, hiding
    every entry and every status in it. Callers normally arrive via
    Path.read_text, which already does this; doing it here too means the
    anchors do not silently depend on how the caller opened the file.
    """

    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_entry_statuses(text: str) -> list[str]:
    """Extract all entry statuses from one markdown block."""

    return normalize_entry_statuses(ENTRY_STATUS_RE.findall(_normalize_newlines(text)))


def parse_daily_entry_statuses(content: str) -> list[DailyEntryStatus]:
    """Parse raw daily markdown and return statuses for each entry in order."""

    content = _normalize_newlines(content)
    matches = list(DAILY_ENTRY_RE.finditer(content))
    parsed: list[DailyEntryStatus] = []
    for index, match in enumerate(matches):
        block_start = match.start()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(
            content
        )
        block = content[block_start:block_end]
        parsed.append(
            DailyEntryStatus(
                time=match.group("time"),
                entry_type=normalize_entry_type(match.group("type")),
                statuses=tuple(extract_entry_statuses(block)),
            )
        )
    return parsed
