"""Markdown rendering, section editing, and human-zone helpers for compiled briefings.

Extracted from ``compiled_briefings.py`` so pure page-structure, human-zone
protection, frontmatter passthrough, and markdown rendering logic lives in one
focused module without circular runtime imports.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from d_brain.clock import owner_today
from d_brain.services import compiled_page
from d_brain.services.compiled_page import (
    FRONTMATTER_RE,
    NOT_ENRICHMENT_SOURCE_MARKER,
    WIKILINK_RE,
)
from d_brain.services.frontmatter import FrontmatterError, parse_frontmatter_bytes

if TYPE_CHECKING:
    from d_brain.services.compiled_briefings import (
        CompiledBatchConsolidationEvent,
        CompiledBriefingTarget,
    )
    from d_brain.services.qmd import QmdService

logger = logging.getLogger(__name__)


class HumanZoneMarkerError(ValueError):
    """Raised when a page's human-zone markers are missing or malformed.

    Subclasses ``ValueError`` (not ``RuntimeError``) on purpose: callers
    already catch ``ValueError`` around briefing writes (``refresh_after_write``,
    ``_backfill_freshness_notes``), so this fails the page closed for free
    without new except clauses — better to skip a page than lose owner text.
    """


STATUS_VALUES = {"active", "draft", "pending", "done", "inactive"}
FRESHNESS_VALUES = {"fresh", "watch", "stale"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
RECORD_KIND_VALUES = {"decision", "incident", "briefing"}
DECISION_STATUS_VALUES = {"proposed", "accepted", "rejected", "superseded"}
INCIDENT_SEVERITY_VALUES = {"low", "medium", "high", "critical"}
SOURCES_TRUST_VALUES = {"own", "forwarded", "integration", "inferred"}
DEFAULT_SOURCES_TRUST = "inferred"

CORE_FRONTMATTER_FIELDS = frozenset(
    {
        "type",
        "domain",
        "aliases",
        "directions",
        "projects",
        "description",
        "status",
        "created",
        "updated",
        "last_compiled_at",
        "freshness_state",
        "confidence",
        "source_count",
        "last_accessed",
        "relevance",
        "tier",
        "sources_trust",
        "last_verified",
        "last_source_checked",
        "enrichment_count",
        "conflicts_open",
        "human_reviewed",
        "human_zone_populated",
        "quality_status",
        "quality_reason",
        "record_kind",
        "decision_status",
        "decision_owner",
        "decision_date",
        "supersedes",
        "superseded_by",
        "incident_date",
        "severity",
        "source_event_dates",
    }
)

MAX_OUTPUT_ARTIFACT_CHARS = 12000

HUMAN_ZONE_START = "<!-- human:start -->"
HUMAN_ZONE_END = "<!-- human:end -->"
EMBEDDED_HEADING_RE = re.compile(r"^(##\s)", re.MULTILINE)
AMBIGUOUS_HUMAN_ZONE = (-1, -1)
OWNER_NOTES_HEADING_RE = re.compile(r"^##\s+Owner Notes\s*$", re.MULTILINE)
HUMAN_ZONE_POPULATED_RE = re.compile(
    r"^human_zone_populated:\s*true\s*$", re.MULTILINE
)

HUMAN_ZONE_DUPLICATE_OVERLAP_THRESHOLD = 0.8
HUMAN_ZONE_DUPLICATE_MIN_TOKENS = 5
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPLICIT_EVENT_DATE_RE = re.compile(
    r"(?im)^(?:event_date|дата\s+события)\s*:\s*[\"']?(\d{4}-\d{2}-\d{2})"
)
SOURCES_SHAPED_TABLE_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*$"
)
CLAIM_HISTORY_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*$"
)
OPEN_CONFLICTS_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*$"
)
DATED_BULLET_ROW_RE = re.compile(
    r"^-\s+(\d{4}-\d{2}-\d{2}):\s+(.*?)(?:\s+\(source:\s*\[\[([^\]]+)\]\]\))?\s*$"
)


def defuse_embedded_headings(text: str) -> str:
    """Indent any line of model-authored text that reads as a page heading."""
    return EMBEDDED_HEADING_RE.sub(r" \1", text)


def human_zone_markers_look_corrupted(text: str) -> bool:
    """True if ``text`` shows structural evidence of a human zone that zero
    exact markers should not be possible for.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if OWNER_NOTES_HEADING_RE.search(normalized) is not None:
        return True
    frontmatter_match = FRONTMATTER_RE.match(normalized)
    if frontmatter_match is None:
        return False
    return HUMAN_ZONE_POPULATED_RE.search(frontmatter_match.group(1)) is not None


def clean_line(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return compiled_page.defuse_human_zone_markers(text.strip())


def paragraph(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return defuse_embedded_headings(
        compiled_page.defuse_human_zone_markers(re.sub(r"\n{3,}", "\n\n", text))
    )


def strip_relative_prefix(raw: str) -> str:
    value = str(raw or "").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def normalize_paths(value: Any, *, vault_path: Path) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    project_root = vault_path.parent.resolve()
    for item in value:
        raw = compiled_page.canonical_source_reference(str(item or "").strip())
        if not raw:
            continue
        if raw.startswith("/"):
            absolute_path = Path(raw)
            try:
                raw = absolute_path.relative_to(vault_path).as_posix()
            except ValueError:
                try:
                    raw = absolute_path.relative_to(project_root).as_posix()
                except ValueError:
                    continue
        raw = strip_relative_prefix(raw)
        if raw.startswith("claude/"):
            raw = f".{raw}"
        if raw.startswith("vault/"):
            raw = raw[len("vault/") :]
        if raw.endswith(".md") or "/" in raw:
            paths.append(raw)
    return paths


def merge_paths(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            path = str(item or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            merged.append(path)
    return merged


def render_bullets(lines: list[str], *, empty: str) -> list[str]:
    if not lines:
        return [f"- {empty}"]
    return [f"- {line}" for line in lines]


def render_sources(paths: list[str]) -> list[str]:
    if not paths:
        return ["- (none)"]
    return [f"- [[{path}]]" for path in paths]


def sections_from_text(text: str) -> set[str]:
    return set(re.findall(r"^##\s+(.+?)\s*$", text or "", re.MULTILINE))


def explicit_event_date(text: str) -> str:
    """Return an explicitly labelled event date, never an inferred one."""
    match = EXPLICIT_EVENT_DATE_RE.search(text or "")
    if match is None:
        return ""
    value = match.group(1)
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""
    return value


def source_event_dates(text: str) -> dict[str, str]:
    try:
        raw = parse_frontmatter_bytes(text.encode("utf-8")).fields.get(
            "source_event_dates", {}
        )
    except FrontmatterError:
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for source, event_date in raw.items():
        if not isinstance(source, str) or not isinstance(event_date, str):
            continue
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue
        result[source] = event_date
    return result


def human_zone_span(text: str) -> tuple[int, int] | None:
    """Byte span of the owner's human zone within ``text``, or ``None``."""
    starts = text.count(HUMAN_ZONE_START)
    ends = text.count(HUMAN_ZONE_END)
    if starts == 0 and ends == 0:
        if human_zone_markers_look_corrupted(text):
            return AMBIGUOUS_HUMAN_ZONE
        return None
    if starts != 1 or ends != 1:
        return AMBIGUOUS_HUMAN_ZONE
    start_index = text.find(HUMAN_ZONE_START)
    end_index = text.find(HUMAN_ZONE_END)
    if end_index < start_index:
        return AMBIGUOUS_HUMAN_ZONE
    return (start_index, end_index + len(HUMAN_ZONE_END))


def heading_match(
    text: str,
    pattern: re.Pattern[str],
    zone: tuple[int, int] | None,
    *,
    start: int = 0,
) -> re.Match[str] | None:
    """First match of ``pattern`` at or after ``start``, skipping ``zone``."""
    for match in pattern.finditer(text, start):
        if zone is not None and zone[0] <= match.start() < zone[1]:
            continue
        return match
    return None


def section_text(text: str, heading: str) -> str:
    text = text or ""
    zone = human_zone_span(text)
    if zone == AMBIGUOUS_HUMAN_ZONE:
        return ""
    heading_pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE
    )
    match = heading_match(text, heading_pattern, zone)
    if match is None:
        return ""
    boundary_pattern = re.compile(r"^##\s+", re.MULTILINE)
    boundary_match = heading_match(
        text, boundary_pattern, zone, start=match.end()
    )
    body_end = boundary_match.start() if boundary_match is not None else len(text)
    if zone is not None and match.end() <= zone[0] < body_end:
        body_end = zone[0]
    return text[match.end() : body_end].strip()


def section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Return the exact section slice, excluding any relocated human zone."""
    zone = human_zone_span(text)
    if zone == AMBIGUOUS_HUMAN_ZONE:
        return None
    heading_pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE
    )
    match = heading_match(text, heading_pattern, zone)
    if match is None:
        return None
    boundary_match = heading_match(
        text,
        re.compile(r"^##\s+", re.MULTILINE),
        zone,
        start=match.end(),
    )
    end = boundary_match.start() if boundary_match is not None else len(text)
    if zone is not None and match.end() <= zone[0] < end:
        end = zone[0]
    return match.start(), end


def section_bullets(text: str, heading: str) -> list[str]:
    section = section_text(text, heading)
    return [
        line[2:].strip()
        for line in section.splitlines()
        if line.startswith("- ") and line[2:].strip()
    ]


def replace_section(text: str, heading: str, new_lines: list[str]) -> str:
    """Replace one ``## heading`` section's body in place."""
    zone = human_zone_span(text)
    if zone == AMBIGUOUS_HUMAN_ZONE:
        return text
    heading_pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE
    )
    match = heading_match(text, heading_pattern, zone)
    if match is None:
        return text
    boundary_pattern = re.compile(r"^##\s+", re.MULTILINE)
    boundary_match = heading_match(
        text, boundary_pattern, zone, start=match.end()
    )
    body_end = boundary_match.start() if boundary_match is not None else len(text)
    if zone is not None and match.end() <= zone[0] < body_end:
        body_end = zone[0]
    replacement_body = "\n".join(new_lines) + "\n\n"
    return text[: match.end()] + replacement_body + text[body_end:]


def has_section(text: str, heading: str) -> bool:
    """Whether ``## heading`` exists on this page, outside the human zone."""
    zone = human_zone_span(text)
    if zone == AMBIGUOUS_HUMAN_ZONE:
        return False
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE)
    return heading_match(text, pattern, zone) is not None


def insert_section_before(
    text: str,
    *,
    heading: str,
    before_heading: str,
    new_lines: list[str],
) -> str:
    """Insert a brand-new ``## heading`` section right before ``before_heading``."""
    zone = human_zone_span(text)
    if zone == AMBIGUOUS_HUMAN_ZONE:
        return text
    before_pattern = re.compile(
        rf"^##\s+{re.escape(before_heading)}\s*$", re.MULTILINE
    )
    before_match = heading_match(text, before_pattern, zone)
    if before_match is None:
        return text
    block = f"## {heading}\n" + "\n".join(new_lines) + "\n\n"
    return text[: before_match.start()] + block + text[before_match.start() :]


def ensure_claim_history_section(text: str) -> str:
    """Put an empty "Claim History" section back on a page that lost it."""
    if has_section(text, "Claim History"):
        return text
    anchor = "History" if has_section(text, "History") else "Owner Notes"
    return insert_section_before(
        text,
        heading="Claim History",
        before_heading=anchor,
        new_lines=compiled_page.render_claim_history([]),
    )


def sources_shaped_rows(text: str) -> list[tuple[str, str, str]]:
    """Parse existing "Sources That Shaped This Page" table rows."""
    section = section_text(text, "Sources That Shaped This Page")
    rows: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        match = SOURCES_SHAPED_TABLE_ROW_RE.match(line.strip())
        if match is None:
            continue
        date_value, source_value, what_value = match.groups()
        source_text = compiled_page.unescape_table_cell(source_value)
        what_text = compiled_page.unescape_table_cell(what_value)
        if what_text == NOT_ENRICHMENT_SOURCE_MARKER:
            continue
        rows.append((date_value, source_text, what_text))
    return rows


def dated_rows(
    text: str,
    heading: str,
    *,
    empty_placeholder: str,
) -> list[tuple[str, str, str]]:
    """Parse a dated-bullet section (Recent Changes / Open Loops / History)."""
    section = section_text(text, heading)
    fallback_date = (
        compiled_page.frontmatter_fields(text).get("updated")
        or owner_today().isoformat()
    )
    rows: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or stripped == f"- {empty_placeholder}":
            continue
        match = DATED_BULLET_ROW_RE.match(stripped)
        if match is not None:
            row_date, row_text, row_source = match.groups()
            rows.append((row_date, row_text.strip(), (row_source or "").strip()))
            continue
        plain_text = stripped[2:].strip()
        if plain_text:
            rows.append((fallback_date, plain_text, ""))
    return rows


def render_dated_bullets(
    rows: list[tuple[str, str, str]],
    *,
    empty: str,
) -> list[str]:
    if not rows:
        return [f"- {empty}"]
    lines = []
    for row_date, row_text, row_source in rows:
        if row_source:
            lines.append(f"- {row_date}: {row_text} (source: [[{row_source}]])")
        else:
            lines.append(f"- {row_date}: {row_text}")
    return lines


def existing_claims_catalog(existing_text: str) -> str:
    """Existing "Sources That Shaped This Page" rows as JSON."""
    rows = sources_shaped_rows(existing_text)
    catalog = [{"source": source, "text": what} for _, source, what in rows]
    return json.dumps(catalog, ensure_ascii=False, indent=2)


def claim_history_rows(text: str) -> list[tuple[str, str, str, str]]:
    """Parse existing "Claim History" table rows."""
    section = section_text(text, "Claim History")
    rows: list[tuple[str, str, str, str]] = []
    for line in section.splitlines():
        match = CLAIM_HISTORY_ROW_RE.match(line.strip())
        if match is None:
            continue
        date_value, source_value, claim_value, superseded_value = match.groups()
        rows.append(
            (
                date_value,
                compiled_page.unescape_table_cell(source_value),
                compiled_page.unescape_table_cell(claim_value),
                compiled_page.unescape_table_cell(superseded_value),
            )
        )
    return rows


def open_conflicts_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    """Parse existing "Open Conflicts" table rows."""
    section = section_text(text, "Open Conflicts")
    rows: list[tuple[str, str, str, str, str]] = []
    for line in section.splitlines():
        match = OPEN_CONFLICTS_ROW_RE.match(line.strip())
        if match is None:
            continue
        date_value, existing_claim, existing_source, new_claim, new_source = (
            match.groups()
        )
        rows.append(
            (
                date_value,
                compiled_page.unescape_table_cell(existing_claim),
                compiled_page.unescape_table_cell(existing_source),
                compiled_page.unescape_table_cell(new_claim),
                compiled_page.unescape_table_cell(new_source),
            )
        )
    return rows


def validated_date_field(
    raw_value: Any,
    *,
    field: str,
    page_path: str,
) -> str:
    """Return a plain YYYY-MM-DD value, or "" (renders as YAML null)."""
    value = compiled_page.metadata_value(raw_value)
    if not value:
        return ""
    if DATE_ONLY_RE.match(value):
        return value
    logger.warning(
        "Compiled briefing %s has invalid %s value %r; clearing it",
        page_path,
        field,
        value,
    )
    return ""


def extra_frontmatter_lines(
    existing_text: str, *, page_path: str
) -> list[str]:
    """Frontmatter lines for owner-layer fields this code does not own."""
    try:
        document = parse_frontmatter_bytes(existing_text.encode("utf-8"))
    except FrontmatterError as exc:
        logger.warning(
            "Compiled briefing %s has frontmatter this rebuild cannot "
            "reparse (%s); owner-layer fields cannot be carried "
            "forward this pass",
            page_path,
            exc,
        )
        return []
    lines: list[str] = []
    for key, value in document.fields.items():
        if key in CORE_FRONTMATTER_FIELDS:
            continue
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning(
                "Compiled briefing %s carries owner-layer field %r "
                "whose value cannot be preserved without corrupting "
                "it; dropping it instead of writing it wrong",
                page_path,
                key,
            )
            continue
        lines.append(f"{key}: {encoded}")
    return lines


def extract_human_zone(text: str) -> str:
    """Extract the human-owned zone verbatim, or build an empty scaffold."""
    body = compiled_page.body_without_frontmatter(text)
    starts = body.count(HUMAN_ZONE_START)
    ends = body.count(HUMAN_ZONE_END)
    if starts == 0 and ends == 0:
        if human_zone_markers_look_corrupted(text):
            raise HumanZoneMarkerError(
                "human zone markers look corrupted: '## Owner Notes' "
                "heading or human_zone_populated frontmatter flag found "
                "but no exact marker present"
            )
        return f"{HUMAN_ZONE_START}\n{HUMAN_ZONE_END}"
    if starts != 1 or ends != 1:
        raise HumanZoneMarkerError(
            "expected exactly one human zone marker pair, found "
            f"start={starts} end={ends}"
        )
    start_index = body.index(HUMAN_ZONE_START)
    end_index = body.index(HUMAN_ZONE_END)
    if end_index < start_index:
        raise HumanZoneMarkerError(
            "human zone end marker appears before the start marker"
        )
    return body[start_index : end_index + len(HUMAN_ZONE_END)]


def float_value(value: str | None, *, default: float) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def text_duplicates_human_zone(
    text: str,
    human_zone_tokens: set[str],
) -> bool:
    if not human_zone_tokens or not text:
        return False
    text_tokens = compiled_page.tokens(text)
    if len(text_tokens) < HUMAN_ZONE_DUPLICATE_MIN_TOKENS:
        return False
    overlap = len(text_tokens & human_zone_tokens)
    return (
        overlap / len(text_tokens)
    ) >= HUMAN_ZONE_DUPLICATE_OVERLAP_THRESHOLD


def filter_list_duplicating_human_zone(
    lines: list[str],
    human_zone_tokens: set[str],
) -> list[str]:
    if not human_zone_tokens:
        return lines
    return [
        line
        for line in lines
        if not text_duplicates_human_zone(line, human_zone_tokens)
    ]


def filter_text_duplicating_human_zone(
    text: str,
    human_zone_tokens: set[str],
) -> str:
    if text_duplicates_human_zone(text, human_zone_tokens):
        return ""
    return text


def apply_partial_sections(
    *,
    existing_text: str,
    rendered: str,
    payload: dict[str, Any],
) -> str:
    """Keep unmentioned existing content sections byte-for-byte intact."""
    if not existing_text:
        return rendered
    if "changed_sections" not in payload:
        raise ValueError("changed_sections is required for an existing briefing")
    changed_sections = payload["changed_sections"]
    if not isinstance(changed_sections, list) or any(
        not isinstance(heading, str) for heading in changed_sections
    ):
        raise ValueError("changed_sections must be a list of section headings")
    if len(changed_sections) != len(set(changed_sections)):
        raise ValueError("changed_sections must not repeat a section heading")

    content_headings = {
        "Current State",
        "Recent Changes",
        "Open Loops",
        "Key Decisions",
        "Next Check",
        "Decision Record",
        "Rationale",
        "Alternatives Considered",
        "Decision Evidence",
        "Incident Debrief",
        "Timeline",
        "Root Cause",
        "What Worked",
        "What Did Not Work",
        "Corrective Actions",
        "Generalizable Learning",
    }
    requested = set(changed_sections)
    if not requested.issubset(content_headings):
        raise ValueError("changed_sections contains a protected or unknown section")
    if any(
        not has_section(existing_text, heading)
        or not has_section(rendered, heading)
        for heading in requested
    ):
        raise ValueError("changed_sections must name existing rendered sections")

    preserved_by_model = requested | {
        "Sources",
        "Sources That Shaped This Page",
        "Open Conflicts",
        "Claim History",
        "Owner Notes",
        "Related Pages",
    }
    existing_headings = sections_from_text(existing_text)
    new_content_headings = (
        sections_from_text(rendered) - existing_headings
    ) & content_headings
    if new_content_headings:
        raise ValueError(
            "partial update must not add content sections: "
            + ", ".join(sorted(new_content_headings))
        )
    for heading in existing_headings:
        if heading in preserved_by_model:
            continue
        existing_span = section_span(existing_text, heading)
        rendered_span = section_span(rendered, heading)
        if existing_span is None or rendered_span is None:
            raise ValueError("partial update cannot preserve an existing section")
        rendered = (
            rendered[: rendered_span[0]]
            + existing_text[existing_span[0] : existing_span[1]]
            + rendered[rendered_span[1] :]
        )
    return rendered


class CompiledRendererMixin:
    if TYPE_CHECKING:
        vault_path: Path
        qmd: QmdService

        def _now(self) -> datetime: ...
        def _extract_human_zone(self, text: str) -> str: ...
        def _filter_list_duplicating_human_zone(
            self, lines: list[str], human_zone_tokens: set[str]
        ) -> list[str]: ...
        def _filter_text_duplicating_human_zone(
            self, text: str, human_zone_tokens: set[str]
        ) -> str: ...
        def _target_path(self, target: CompiledBriefingTarget) -> Path: ...
        def _clean_line(self, value: Any) -> str: ...
        def _paragraph(self, value: Any) -> str: ...
        def _validated_date_field(
            self, raw_value: Any, *, field: str, page_path: str
        ) -> str: ...
        def _normalize_paths(self, value: Any) -> list[str]: ...
        def _section_text(self, text: str, heading: str) -> str: ...
        def _section_bullets(self, text: str, heading: str) -> list[str]: ...
        def _dated_rows(
            self, text: str, heading: str, *, empty_placeholder: str
        ) -> list[tuple[str, str, str]]: ...
        def _merge_paths(self, *groups: list[str]) -> list[str]: ...
        def _merged_relevance(
            self, existing_meta: dict[str, str], signal: dict[str, Any] | None
        ) -> float: ...
        def _merged_tier(
            self, existing_meta: dict[str, str], signal: dict[str, Any] | None
        ) -> str: ...
        def _sources_shaped_rows(
            self, text: str
        ) -> list[tuple[str, str, str]]: ...
        def _source_event_dates(self, text: str) -> dict[str, str]: ...
        def _claim_history_rows(
            self, text: str
        ) -> list[tuple[str, str, str, str]]: ...
        def _open_conflicts_rows(
            self, text: str
        ) -> list[tuple[str, str, str, str, str]]: ...
        def _apply_claims_and_conflicts(
            self,
            *,
            claims: list[dict[str, str]],
            conflicts: list[dict[str, str]],
            shaped_rows: list[tuple[str, str, str]],
            claim_history_rows: list[tuple[str, str, str, str]],
            open_conflict_rows: list[tuple[str, str, str, str, str]],
            source_rel_path: str,
            source_excerpt: str,
            signal: dict[str, Any] | None,
            today: str,
            page_rel_path: str,
            page_state: str = "",
            payload: dict[str, Any] | None = None,
            source_event_dates: dict[str, str] | None = None,
            record_side_effects: bool = True,
            adjudication_cache: dict[tuple[str, str, str, str, str], tuple[str, str]]
            | None = None,
        ) -> tuple[
            list[tuple[str, str, str]],
            list[tuple[str, str, str, str]],
            list[tuple[str, str, str, str, str]],
        ]: ...
        def _source_trust_level(
            self, source_rel_path: str, source_excerpt: str
        ) -> str: ...
        def _event_date_for_source(
            self, source_rel_path: str, excerpt: str = ""
        ) -> str: ...
        def _target_metadata(
            self,
            target: CompiledBriefingTarget,
            existing_text: str,
            page_path: str,
        ) -> dict[str, list[str]]: ...
        def _extra_frontmatter_lines(
            self, existing_text: str, *, page_path: str
        ) -> list[str]: ...
        def _render_bullets(
            self, lines: list[str], *, empty: str
        ) -> list[str]: ...
        def _render_sources(self, paths: list[str]) -> list[str]: ...
        def _render_dated_bullets(
            self, rows: list[tuple[str, str, str]], *, empty: str
        ) -> list[str]: ...
        def _human_zone_span(self, text: str) -> tuple[int, int] | None: ...

    def _render_briefing(
        self,
        *,
        target: CompiledBriefingTarget,
        payload: dict[str, Any],
        source_rel_path: str,
        existing_text: str,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
        source_excerpt: str = "",
        claims: list[dict[str, str]] | None = None,
        conflicts: list[dict[str, str]] | None = None,
        record_side_effects: bool = True,
        adjudication_cache: dict[tuple[str, str, str, str, str], tuple[str, str]]
        | None = None,
        compiled_at: str | None = None,
    ) -> str:
        """Render one compiled briefing markdown page with frontmatter."""
        claims = claims or []
        conflicts = conflicts or []

        human_zone = self._extract_human_zone(existing_text)
        human_zone_inner = human_zone[
            len(HUMAN_ZONE_START) : len(human_zone) - len(HUMAN_ZONE_END)
        ].strip()
        human_zone_tokens = (
            compiled_page.tokens(human_zone_inner) if human_zone_inner else set()
        )
        human_zone_populated = bool(human_zone_inner) or (
            existing_meta.get("human_zone_populated") == "true"
        )

        if claims and human_zone_tokens:
            kept_claim_texts = set(
                self._filter_list_duplicating_human_zone(
                    [claim["text"] for claim in claims], human_zone_tokens
                )
            )
            claims = [claim for claim in claims if claim["text"] in kept_claim_texts]
            conflicts = [
                conflict
                for conflict in conflicts
                if conflict["new_claim"] in kept_claim_texts
            ]

        today = owner_today().isoformat()
        compiled_at_value = (
            compiled_at
            if compiled_at is not None
            else self._now().isoformat(timespec="seconds")
        )
        page_rel_path = (
            self._target_path(target).relative_to(self.vault_path).as_posix()
        )
        description = self._clean_line(payload.get("description")) or target.description
        status = str(payload.get("status") or existing_meta.get("status") or "active")
        if status not in STATUS_VALUES:
            status = "active"
        freshness_state = str(
            payload.get("freshness_state")
            or existing_meta.get("freshness_state")
            or "watch"
        )
        if freshness_state not in FRESHNESS_VALUES:
            freshness_state = "watch"
        confidence = str(
            payload.get("confidence")
            or existing_meta.get("confidence")
            or "medium"
        )
        if confidence not in CONFIDENCE_VALUES:
            confidence = "medium"
        quality_status = existing_meta.get("quality_status", "").strip()
        quality_reason = existing_meta.get("quality_reason", "").strip()
        if payload.get("_quality_verification_completed") is True:
            quality_issues = payload.get("_quality_issues")
            if isinstance(quality_issues, list) and quality_issues:
                quality_status = "needs_review"
                quality_reason = "; ".join(
                    str(issue).strip()
                    for issue in quality_issues
                    if str(issue).strip()
                )
            else:
                quality_status = ""
                quality_reason = ""

        current_state = self._paragraph(payload.get("current_state"))

        def without_leading_date(items: list[str]) -> list[str]:
            cleaned = [
                re.sub(r"^\d{4}-\d{2}-\d{2}:\s*", "", item).strip()
                for item in items
            ]
            return [item for item in cleaned if item]

        recent_changes = without_leading_date(
            compiled_page.normalize_list(payload.get("recent_changes"))
        )
        open_loops = without_leading_date(
            compiled_page.normalize_list(payload.get("open_loops"))
        )
        key_decisions = self._filter_list_duplicating_human_zone(
            compiled_page.normalize_list(payload.get("key_decisions")),
            human_zone_tokens,
        )
        next_check = defuse_embedded_headings(
            self._clean_line(payload.get("next_check"))
        )

        record_kind = ""
        decision_status = ""
        decision_owner = ""
        decision_date = ""
        rationale = ""
        alternatives: list[str] = []
        supersedes: list[str] = []
        superseded_by = ""
        decision_evidence: list[str] = []
        incident_date = ""
        severity = ""
        timeline: list[str] = []
        root_cause = ""
        what_worked: list[str] = []
        what_did_not_work: list[str] = []
        corrective_actions: list[str] = []
        generalizable_learning = ""
        if target.domain == "decisions":
            existing_record_kind = compiled_page.metadata_value(
                existing_meta.get("record_kind")
            )
            existing_decision_status = compiled_page.metadata_value(
                existing_meta.get("decision_status")
            )
            record_kind = compiled_page.metadata_value(
                payload.get("record_kind")
                or existing_record_kind
                or "decision"
            )
            if (
                existing_record_kind == "decision"
                and existing_decision_status == "accepted"
            ):
                record_kind = "decision"
            if record_kind not in RECORD_KIND_VALUES:
                record_kind = "decision"
            if record_kind == "decision":
                decision_status = compiled_page.metadata_value(
                    payload.get("decision_status")
                    or existing_meta.get("decision_status")
                    or "proposed"
                )
                if decision_status not in DECISION_STATUS_VALUES:
                    decision_status = "proposed"
                decision_owner = compiled_page.metadata_value(
                    payload.get("decision_owner")
                    or existing_meta.get("decision_owner")
                )
                decision_date = self._validated_date_field(
                    payload.get("decision_date")
                    or existing_meta.get("decision_date"),
                    field="decision_date",
                    page_path=f"compiled/{target.domain}/{target.slug}.md",
                )
                rationale = self._filter_text_duplicating_human_zone(
                    self._paragraph(payload.get("rationale")), human_zone_tokens
                )
                alternatives = self._filter_list_duplicating_human_zone(
                    compiled_page.normalize_list(payload.get("alternatives_considered")),
                    human_zone_tokens,
                )
                supersedes = compiled_page.normalize_list(payload.get("supersedes"))
                superseded_by = self._clean_line(payload.get("superseded_by"))
                decision_evidence = self._normalize_paths(
                    payload.get("decision_evidence")
                )

                explicit_supersession = (
                    decision_status == "superseded" and bool(superseded_by)
                )
                if existing_decision_status == "accepted" and not explicit_supersession:
                    decision_status = "accepted"
                    decision_owner = compiled_page.metadata_value(
                        existing_meta.get("decision_owner")
                    ) or decision_owner
                    decision_date = self._validated_date_field(
                        existing_meta.get("decision_date"),
                        field="decision_date",
                        page_path=f"compiled/{target.domain}/{target.slug}.md",
                    ) or decision_date
                    rationale = (
                        self._section_text(existing_text, "Rationale") or rationale
                    )
                    alternatives = self._section_bullets(
                        existing_text,
                        "Alternatives Considered",
                    ) or alternatives
                    key_decisions = self._section_bullets(
                        existing_text,
                        "Key Decisions",
                    ) or key_decisions
                if decision_status == "accepted" and not key_decisions:
                    key_decisions = [target.title]
            elif record_kind == "incident":
                incident_date = self._validated_date_field(
                    payload.get("incident_date")
                    or existing_meta.get("incident_date"),
                    field="incident_date",
                    page_path=f"compiled/{target.domain}/{target.slug}.md",
                )
                severity = compiled_page.metadata_value(
                    payload.get("severity") or existing_meta.get("severity")
                )
                if severity and severity not in INCIDENT_SEVERITY_VALUES:
                    logger.warning(
                        "Compiled briefing %s has invalid severity value %r; "
                        "clearing it",
                        f"compiled/{target.domain}/{target.slug}.md",
                        severity,
                    )
                    severity = ""
                timeline = compiled_page.normalize_list(payload.get("timeline"))
                root_cause = self._paragraph(payload.get("root_cause"))
                what_worked = compiled_page.normalize_list(payload.get("what_worked"))
                what_did_not_work = compiled_page.normalize_list(
                    payload.get("what_did_not_work")
                )
                corrective_actions = compiled_page.normalize_list(
                    payload.get("corrective_actions")
                )
                generalizable_learning = self._paragraph(
                    payload.get("generalizable_learning")
                )

        recent_changes = self._filter_list_duplicating_human_zone(
            recent_changes, human_zone_tokens
        )
        open_loops = self._filter_list_duplicating_human_zone(
            open_loops, human_zone_tokens
        )

        source_date_value = self.qmd._record_date_for_rel_path(
            source_rel_path, signal
        )
        source_date = (
            source_date_value.isoformat() if source_date_value is not None else today
        )
        recent_changes_rows = self._dated_rows(
            existing_text,
            "Recent Changes",
            empty_placeholder="No recent changes captured yet.",
        )
        existing_recent_pairs = {
            (row_text, row_source) for _, row_text, row_source in recent_changes_rows
        }
        for item in recent_changes:
            pair = (item, source_rel_path)
            if pair not in existing_recent_pairs:
                recent_changes_rows.append((source_date, item, source_rel_path))
                existing_recent_pairs.add(pair)

        open_loops_rows = self._dated_rows(
            existing_text,
            "Open Loops",
            empty_placeholder="No open loops captured yet.",
        )
        existing_open_pairs = {
            (row_text, row_source) for _, row_text, row_source in open_loops_rows
        }
        for item in open_loops:
            pair = (item, source_rel_path)
            if pair not in existing_open_pairs:
                open_loops_rows.append((source_date, item, source_rel_path))
                existing_open_pairs.add(pair)

        history_rows = self._dated_rows(
            existing_text, "History", empty_placeholder="(nothing archived yet)"
        )

        timeline = self._filter_list_duplicating_human_zone(
            timeline, human_zone_tokens
        )
        root_cause = self._filter_text_duplicating_human_zone(
            root_cause, human_zone_tokens
        )
        what_worked = self._filter_list_duplicating_human_zone(
            what_worked, human_zone_tokens
        )
        what_did_not_work = self._filter_list_duplicating_human_zone(
            what_did_not_work, human_zone_tokens
        )
        corrective_actions = self._filter_list_duplicating_human_zone(
            corrective_actions, human_zone_tokens
        )
        generalizable_learning = self._filter_text_duplicating_human_zone(
            generalizable_learning, human_zone_tokens
        )

        old_sources = compiled_page.source_links_from_note(existing_text)
        new_sources = self._normalize_paths(payload.get("source_links"))
        all_sources = self._merge_paths(old_sources, [source_rel_path, *new_sources])
        created = existing_meta.get("created") or today
        relevance = self._merged_relevance(existing_meta, signal)
        tier = self._merged_tier(existing_meta, signal)

        shaped_rows = self._sources_shaped_rows(existing_text)
        source_event_dates_map = self._source_event_dates(existing_text)
        related_pages = self._section_bullets(existing_text, "Related Pages")
        claim_history_rows_val = self._claim_history_rows(existing_text)
        open_conflict_rows_before = self._open_conflicts_rows(existing_text)
        open_conflict_rows_val = open_conflict_rows_before
        if claims:
            shaped_rows, claim_history_rows_val, open_conflict_rows_val = (
                self._apply_claims_and_conflicts(
                    claims=claims,
                    conflicts=conflicts,
                    shaped_rows=shaped_rows,
                    claim_history_rows=claim_history_rows_val,
                    open_conflict_rows=open_conflict_rows_val,
                    source_rel_path=source_rel_path,
                    source_excerpt=source_excerpt,
                    signal=signal,
                    today=today,
                    page_rel_path=page_rel_path,
                    page_state=self._section_text(existing_text, "Current State"),
                    payload=payload,
                    source_event_dates=source_event_dates_map,
                    record_side_effects=record_side_effects,
                    adjudication_cache=adjudication_cache,
                )
            )
            if payload.get("_preserve_existing_current_state"):
                current_state = self._section_text(existing_text, "Current State")
        else:
            what_added = (
                (recent_changes[0] if recent_changes else "")
                or description
                or target.reason
                or "(not captured)"
            )
            if what_added == NOT_ENRICHMENT_SOURCE_MARKER:
                what_added += "."
            existing_shaped_pairs = {(row[0], row[1]) for row in shaped_rows}
            if (source_date, source_rel_path) not in existing_shaped_pairs:
                shaped_rows = [
                    *shaped_rows,
                    (source_date, source_rel_path, what_added),
                ]
        new_open_conflicts = len(open_conflict_rows_val) - len(
            open_conflict_rows_before
        )

        existing_sources_trust = existing_meta.get("sources_trust", "").strip()
        if claims:
            current_trust = self._source_trust_level(source_rel_path, source_excerpt)
            if existing_sources_trust in SOURCES_TRUST_VALUES:
                sources_trust = compiled_page.min_trust(
                    existing_sources_trust, current_trust
                )
            else:
                sources_trust = current_trust
        elif existing_sources_trust in SOURCES_TRUST_VALUES:
            sources_trust = existing_sources_trust
        else:
            sources_trust = DEFAULT_SOURCES_TRUST
        page_path = f"compiled/{target.domain}/{target.slug}.md"
        last_verified = self._validated_date_field(
            existing_meta.get("last_verified"),
            field="last_verified",
            page_path=page_path,
        )
        if claims and quality_status != "needs_review":
            last_verified = today
        enrichment_count = (
            compiled_page.int_value(existing_meta.get("enrichment_count"), default=0)
            + 1
        )
        conflicts_open = max(
            0,
            compiled_page.int_value(existing_meta.get("conflicts_open"), default=0)
            + new_open_conflicts,
        )
        if open_conflict_rows_val and confidence == "high":
            confidence = "medium"
        shaped_source_count = len(
            {
                source
                for _, source, what_added in shaped_rows
                if what_added != NOT_ENRICHMENT_SOURCE_MARKER
            }
        )
        human_reviewed = self._validated_date_field(
            existing_meta.get("human_reviewed"),
            field="human_reviewed",
            page_path=page_path,
        )
        event_date = self._event_date_for_source(source_rel_path, source_excerpt)
        if event_date:
            source_event_dates_map[source_rel_path] = event_date

        lines = [
            "---",
            "type: compiled-briefing",
            f"domain: {target.domain}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            f"status: {status}",
            f"created: {created}",
            f"updated: {today}",
            f"last_compiled_at: {compiled_at_value}",
            f"freshness_state: {freshness_state}",
            f"confidence: {confidence}",
            f"source_count: {shaped_source_count}",
            f"last_accessed: {today}",
            f"relevance: {relevance:.2f}",
            f"tier: {tier}",
            f"sources_trust: {sources_trust}",
            f"last_verified: {last_verified}",
            "last_source_checked: "
            + self._validated_date_field(
                existing_meta.get("last_source_checked"),
                field="last_source_checked",
                page_path=page_path,
            ),
            f"enrichment_count: {enrichment_count}",
            f"conflicts_open: {conflicts_open}",
            f"human_reviewed: {human_reviewed}",
        ]
        metadata = self._target_metadata(target, existing_text, page_path)
        aliases, directions, projects = (
            metadata["aliases"],
            metadata["directions"],
            metadata["projects"],
        )
        if aliases and target.domain in {"people", "projects"}:
            lines.append("aliases: " + json.dumps(aliases, ensure_ascii=False))
        if directions:
            lines.append("directions: " + json.dumps(directions, ensure_ascii=False))
        if projects:
            lines.append("projects: " + json.dumps(projects, ensure_ascii=False))
            related_pages = list(
                dict.fromkeys(
                    (
                        *related_pages,
                        *[f"[[{path}]] — основной проект" for path in projects],
                    )
                )
            )
        if human_zone_populated:
            lines.append("human_zone_populated: true")
        if source_event_dates_map:
            lines.append(
                "source_event_dates: "
                + json.dumps(source_event_dates_map, ensure_ascii=False, sort_keys=True)
            )
        if quality_status == "needs_review" and quality_reason:
            lines.extend(
                [
                    "quality_status: needs_review",
                    f"quality_reason: {json.dumps(quality_reason, ensure_ascii=False)}",
                ]
            )
        if record_kind:
            lines.append(f"record_kind: {record_kind}")
        if record_kind == "decision":
            lines.extend(
                [
                    f"decision_status: {decision_status}",
                    f"decision_owner: {json.dumps(decision_owner, ensure_ascii=False)}",
                    f"decision_date: {decision_date}",
                    f"supersedes: {json.dumps(supersedes, ensure_ascii=False)}",
                    f"superseded_by: {json.dumps(superseded_by, ensure_ascii=False)}",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    f"incident_date: {incident_date}",
                    f"severity: {severity}",
                ]
            )
        lines.extend(self._extra_frontmatter_lines(existing_text, page_path=page_path))
        lines.extend(["---", "", f"# {target.title}", ""])
        if record_kind == "decision":
            lines.extend(
                [
                    "## Decision Record",
                    f"- Status: {decision_status}",
                    f"- Date: {decision_date or '(unknown)'}",
                    f"- Owner: {decision_owner or '(unknown)'}",
                    *([f"- Supersedes: {', '.join(supersedes)}"] if supersedes else []),
                    *([f"- Superseded by: {superseded_by}"] if superseded_by else []),
                    "",
                    "## Rationale",
                    rationale or "No rationale captured yet.",
                    "",
                    "## Alternatives Considered",
                    *self._render_bullets(
                        alternatives,
                        empty="No alternatives captured yet.",
                    ),
                    "",
                    "## Decision Evidence",
                    *self._render_sources(decision_evidence),
                    "",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    "## Incident Debrief",
                    f"- Date: {incident_date or '(unknown)'}",
                    f"- Severity: {severity or '(unknown)'}",
                    "",
                    "## Timeline",
                    *self._render_bullets(timeline, empty="No timeline captured yet."),
                    "",
                    "## Root Cause",
                    root_cause or "No reliable root cause captured yet.",
                    "",
                    "## What Worked",
                    *self._render_bullets(
                        what_worked,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## What Did Not Work",
                    *self._render_bullets(
                        what_did_not_work,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## Corrective Actions",
                    *self._render_bullets(
                        corrective_actions,
                        empty="No corrective actions captured yet.",
                    ),
                    "",
                    "## Generalizable Learning",
                    generalizable_learning or "No reusable learning captured yet.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Current State",
                current_state or "No reliable current state yet.",
                "",
                "## Recent Changes",
                *self._render_dated_bullets(
                    recent_changes_rows,
                    empty="No recent changes captured yet.",
                ),
                "",
                "## Open Loops",
                *self._render_dated_bullets(
                    open_loops_rows,
                    empty="No open loops captured yet.",
                ),
                "",
                "## Key Decisions",
                *self._render_bullets(
                    key_decisions,
                    empty="No key decisions captured yet.",
                ),
                "",
                "## Next Check",
                next_check or "Review after the next meaningful update.",
                "",
            ]
        )
        if related_pages:
            lines.extend(
                [
                    "## Related Pages",
                    *self._render_bullets(related_pages, empty="(none yet)"),
                    "",
                ]
            )
        lines.extend(
            [
                "## Sources",
                *self._render_sources(all_sources),
                "",
                "## Sources That Shaped This Page",
                *compiled_page.render_sources_shaped_table(shaped_rows),
                "",
                "## Open Conflicts",
                *compiled_page.render_open_conflicts_table(open_conflict_rows_val),
                "",
                "## Claim History",
                *compiled_page.render_claim_history(claim_history_rows_val),
                "",
            ]
        )
        if history_rows:
            lines.extend(
                [
                    "## History",
                    *self._render_dated_bullets(
                        history_rows, empty="(nothing archived yet)"
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "## Owner Notes",
                human_zone,
                "",
            ]
        )
        rendered = "\n".join(lines)
        human_span = self._human_zone_span(rendered)
        if human_span is AMBIGUOUS_HUMAN_ZONE:
            raise HumanZoneMarkerError(
                "rendered page would carry ambiguous human zone markers: "
                f"start={rendered.count(HUMAN_ZONE_START)} "
                f"end={rendered.count(HUMAN_ZONE_END)}"
            )

        def canonical_link(match: re.Match[str]) -> str:
            if (
                human_span is not None
                and human_span[0] <= match.start() < human_span[1]
            ):
                return match.group(0)
            return match.group(0).replace(
                match.group(1),
                compiled_page.canonical_source_reference(match.group(1)),
                1,
            )

        return WIKILINK_RE.sub(canonical_link, rendered)



def artifact_title(request: str) -> str:
    first = " ".join(str(request or "").split()).strip()
    if not first:
        return "Assistant Output"
    return first[:80].rstrip(" .!?") or "Assistant Output"


def render_output_artifact(
    *,
    title: str,
    request: str,
    output_markdown: str,
    artifact_type: str,
    created_at: datetime,
    max_chars: int = MAX_OUTPUT_ARTIFACT_CHARS,
) -> str:
    body = compiled_page.clip(output_markdown.strip(), max_chars)
    return (
        "---\n"
        f"date: {created_at.date().isoformat()}\n"
        "type: assistant-output\n"
        f"description: {json.dumps(title, ensure_ascii=False)}\n"
        f"artifact_type: {artifact_type}\n"
        f"created: {created_at.date().isoformat()}\n"
        f"updated: {created_at.date().isoformat()}\n"
        f"last_accessed: {created_at.date().isoformat()}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Request\n"
        f"{request.strip()}\n\n"
        "## Output\n"
        f"{body}\n"
    )


def render_batch_consolidation(
    *,
    headline: str,
    payload: dict[str, Any],
    events: list[CompiledBatchConsolidationEvent],
    created_at: datetime,
) -> str:
    summary = (
        paragraph(payload.get("summary"))
        or "No durable cross-source pattern captured yet."
    )
    themes = compiled_page.normalize_list(payload.get("themes"))
    follow_ups = compiled_page.normalize_list(payload.get("follow_ups"))
    source_paths = merge_paths([event.source_rel_path for event in events])
    updated_paths = merge_paths(
        *[list(event.updated_paths) for event in events]
    )
    themes_block = "\n".join(
        render_bullets(themes, empty="No strong recurring themes yet.")
    )
    follow_ups_block = "\n".join(
        render_bullets(
            follow_ups,
            empty="No immediate follow-ups captured.",
        )
    )
    updated_block = "\n".join(render_sources(updated_paths))
    sources_block = "\n".join(render_sources(source_paths))
    return (
        "---\n"
        f"date: {created_at.date().isoformat()}\n"
        "type: compiled-consolidation\n"
        f"description: {json.dumps(headline, ensure_ascii=False)}\n"
        "artifact_type: compiled-batch\n"
        f"created: {created_at.date().isoformat()}\n"
        f"updated: {created_at.date().isoformat()}\n"
        f"last_accessed: {created_at.date().isoformat()}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        f"# {headline}\n\n"
        "## Summary\n"
        f"{summary}\n\n"
        "## Themes\n"
        f"{themes_block}\n\n"
        "## Follow-ups\n"
        f"{follow_ups_block}\n\n"
        "## Updated Briefings\n"
        f"{updated_block}\n\n"
        "## Sources\n"
        f"{sources_block}\n"
    )
