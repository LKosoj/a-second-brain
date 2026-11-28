"""Rolling handoff.md parsing, compaction, and atomic update helpers."""

from __future__ import annotations

import logging
import re
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from d_brain.clock import owner_today
from d_brain.services.frontmatter import split_frontmatter_text
from d_brain.services.vault_lock import vault_write_lock

if TYPE_CHECKING:
    from d_brain.services.processor import CliProcessor

logger = logging.getLogger(__name__)

HANDOFF_SECTION_ORDER = (
    "Last Session",
    "Key Decisions",
    "In Progress",
    "Next Steps",
    "Observations",
)
HandoffRevision = tuple[int, int, int, int, bytes]
HANDOFF_BULLET_SECTIONS = frozenset(HANDOFF_SECTION_ORDER[1:])
HANDOFF_EMPTY_SECTION = {
    "Last Session": "(none)",
    "Key Decisions": "- (none)",
    "In Progress": "- (none)",
    "Next Steps": "- (none)",
    "Observations": "- (none)",
}
HANDOFF_SECTION_RE = re.compile(
    r"^##\s+(Last Session|Key Decisions|In Progress|Next Steps|Observations)\s*$",
    re.MULTILINE,
)


def handoff_section_spans(text: str) -> list[tuple[str, int, int]]:
    """Canonical handoff sections of ``text`` as (title, body_start, body_end).

    A body runs from the end of its heading to the next canonical heading (or
    the end of the text). Offsets index ``text`` as given, no newline
    normalization; text before the first heading belongs to no section.
    """

    matches = list(HANDOFF_SECTION_RE.finditer(text))
    ends = [m.start() for m in matches[1:]] + [len(text)]
    spans = zip(matches, ends[: len(matches)], strict=True)
    return [(m.group(1), m.end(), end) for m, end in spans]


def parse_handoff_sections(body: str) -> dict[str, list[str]]:
    """Collect all canonical handoff sections in encounter order."""
    sections: dict[str, list[str]] = {title: [] for title in HANDOFF_SECTION_ORDER}
    for title, start, end in handoff_section_spans(body):
        sections[title].append(body[start:end].strip())
    return sections


def normalize_handoff_last_session(chunks: list[str]) -> str:
    """Collapse the latest last-session block into one compact paragraph."""
    for chunk in reversed(chunks):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if lines:
            return " ".join(lines)
    return HANDOFF_EMPTY_SECTION["Last Session"]


def coerce_bullet_lines(content: str) -> list[str]:
    """Normalize arbitrary lines into flat markdown bullets."""
    bullets: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line in {"(none)", "- (none)"}:
            continue
        line = re.sub(r"^\d+\.\s+", "", line)
        if not line.startswith("- "):
            line = f"- {line.lstrip('-').strip()}"
        bullets.append(line)
    return bullets


def normalize_handoff_bullet_section(chunks: list[str]) -> str:
    """Keep the latest non-empty bullet section in canonical form."""
    for chunk in reversed(chunks):
        bullets = coerce_bullet_lines(chunk)
        if not bullets:
            continue
        deduped = list(dict.fromkeys(bullets))
        return "\n".join(deduped)
    return "- (none)"


def normalize_handoff_observations(chunks: list[str]) -> str:
    """Deduplicate observations without dropping older surviving items."""
    collected: list[str] = []
    for chunk in chunks:
        collected.extend(coerce_bullet_lines(chunk))
    if not collected:
        return HANDOFF_EMPTY_SECTION["Observations"]

    deduped_reversed: list[str] = []
    seen: set[str] = set()
    for bullet in reversed(collected):
        if bullet in seen:
            continue
        seen.add(bullet)
        deduped_reversed.append(bullet)
    deduped = list(reversed(deduped_reversed))[-10:]
    return "\n".join(deduped)


def default_handoff_frontmatter() -> str:
    """Frontmatter for a freshly initialized rolling handoff file."""
    today = owner_today().isoformat()
    return (
        "---\n"
        "type: note\n"
        f"last_accessed: {today}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
    )


def normalized_handoff_sections(text: str) -> tuple[str, dict[str, str]]:
    """Parse and normalize all canonical handoff sections."""
    frontmatter, body = split_frontmatter_text(text)
    sections = parse_handoff_sections(body)
    if not frontmatter:
        frontmatter = default_handoff_frontmatter()

    normalized_sections = {
        "Last Session": normalize_handoff_last_session(sections["Last Session"]),
        "Key Decisions": normalize_handoff_bullet_section(sections["Key Decisions"]),
        "In Progress": normalize_handoff_bullet_section(sections["In Progress"]),
        "Next Steps": normalize_handoff_bullet_section(sections["Next Steps"]),
        "Observations": normalize_handoff_observations(sections["Observations"]),
    }
    return frontmatter, normalized_sections


def render_handoff_sections(frontmatter: str, sections: dict[str, str]) -> str:
    """Render one canonical handoff document from normalized sections."""
    lines = [frontmatter.rstrip(), "# Передача сессии", ""]
    for title in HANDOFF_SECTION_ORDER:
        lines.append(f"## {title}")
        lines.append(sections[title])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def compact_handoff_text(text: str) -> str:
    """Render handoff into one canonical rolling document."""
    frontmatter, normalized_sections = normalized_handoff_sections(text)
    return render_handoff_sections(frontmatter, normalized_sections)


def handoff_revision(path: Path, content: bytes) -> HandoffRevision:
    """Identify one exact atomic handoff version, including same-byte rewrites."""
    file_stat = path.stat()
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mtime_ns,
        file_stat.st_size,
        sha256(content).digest(),
    )


def splice_handoff_last_session(text: str, sentence: str) -> str:
    """Add one sentence to the "Last Session" block, leaving every other
    byte of the handoff exactly as it was.

    Appends rather than replaces: the daily reflect phase writes that
    section earlier in the same scheduled run, and a weekly cycle that
    overwrote it would drop the day's own summary from the only file the
    next session reads for continuity. Re-appending the same sentence is
    a no-op, so a cycle that runs twice on one day does not say the same
    thing twice.

    Splices instead of re-rendering the whole document on purpose: the
    rendering path normalizes every other section too (deduping
    observations, among other things), which is the right thing for
    ``_compact_handoff_file`` and the wrong thing for a write whose only
    business is one sentence -- see
    ``_merge_handoff_observation_snapshot``, which deliberately leaves a
    concurrently edited handoff alone.
    """
    for title, start, end in handoff_section_spans(text):
        if title != "Last Session":
            continue
        current = text[start:end].strip()
        if sentence in current:
            return text
        if not current or current == HANDOFF_EMPTY_SECTION["Last Session"]:
            merged = sentence
        else:
            merged = current + " " + sentence
        tail = text[end:]
        separator = "\n\n" if tail else "\n"
        return text[:start] + "\n" + merged + separator + tail
    return text


def ensure_handoff_file(host: CliProcessor) -> None:
    """Create handoff stub expected by the reflect phase."""
    handoff_path = host._handoff_path()
    with vault_write_lock(host.vault_path) as lock:
        if handoff_path.exists():
            return
        host._write_vault_markdown(
            handoff_path,
            host._compact_handoff_text(""),
            lock=lock,
        )
    logger.info("Created handoff stub at %s", handoff_path)


def compact_handoff_file(host: CliProcessor) -> None:
    """Normalize handoff into its rolling compact form."""
    handoff_path = host._handoff_path()
    with vault_write_lock(host.vault_path) as lock:
        if not handoff_path.exists():
            return
        compacted = host._compact_handoff_text(
            handoff_path.read_text(encoding="utf-8")
        )
        host._write_vault_markdown(handoff_path, compacted, lock=lock)


def handoff_observation_snapshot(
    host: CliProcessor,
) -> tuple[list[str], HandoffRevision]:
    """Read observations and their exact handoff revision under a short lock."""
    host._ensure_handoff_file()
    handoff_path = host._handoff_path()
    with vault_write_lock(host.vault_path):
        content = handoff_path.read_bytes()
        revision = host._handoff_revision(handoff_path, content)
    _, sections = host._normalized_handoff_sections(content.decode("utf-8"))
    observations = host._coerce_bullet_lines(sections["Observations"])
    return observations, revision


def append_handoff_last_session(host: CliProcessor, sentence: str) -> None:
    """Record one sentence in the rolling handoff "Last Session" section."""
    text = " ".join(str(sentence or "").split())
    if not text:
        return
    host._ensure_handoff_file()
    handoff_path = host._handoff_path()
    with vault_write_lock(host.vault_path) as lock:
        current = handoff_path.read_text(encoding="utf-8")
        updated = host._splice_handoff_last_session(current, text)
        if updated == current:
            return
        host._write_vault_markdown(handoff_path, updated, lock=lock)


def merge_handoff_observation_snapshot(
    host: CliProcessor,
    snapshot: list[str],
    carry_forward: list[str],
    snapshot_revision: HandoffRevision,
) -> tuple[list[str], int]:
    """Apply an unchanged snapshot or conservatively retain ambiguous bullets."""
    snapshot_bullets = host._coerce_bullet_lines("\n".join(snapshot))
    carried = set(host._coerce_bullet_lines("\n".join(carry_forward)))
    processed = {bullet for bullet in snapshot_bullets if bullet not in carried}

    host._ensure_handoff_file()
    handoff_path = host._handoff_path()
    with vault_write_lock(host.vault_path) as lock:
        current_bytes = handoff_path.read_bytes()
        revision_matches = (
            host._handoff_revision(handoff_path, current_bytes) == snapshot_revision
        )
        frontmatter, sections = host._normalized_handoff_sections(
            current_bytes.decode("utf-8")
        )
        current = host._coerce_bullet_lines(sections["Observations"])
        if not revision_matches:
            return current, 0
        merged = [bullet for bullet in current if bullet not in processed]
        processed_count = len(processed)
        sections["Observations"] = (
            "\n".join(merged) if merged else HANDOFF_EMPTY_SECTION["Observations"]
        )
        host._write_vault_markdown(
            handoff_path,
            host._render_handoff_sections(frontmatter, sections),
            lock=lock,
        )
    return merged, processed_count
