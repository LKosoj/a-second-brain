"""Compiled-page helpers: pure functions plus thin wrappers over service sections.

Everything here works on plain text or paths and holds no service state, so
the neighbouring ``compiled_*`` modules can use it without reaching into
``CompiledBriefingService`` internals. It must not import ``compiled_briefings``
at module level (that module imports this one).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from d_brain.services.frontmatter import FrontmatterError, parse_frontmatter_bytes

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(
    r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_-]{2,}"
)
# A leading BOM (\ufeff) is tolerated: e.g. Notepad's "Save As
# UTF-8" on Windows writes one (the owner edits these files by hand).
# Without it, `^` (no re.MULTILINE) never matches and every caller below
# silently treats the whole frontmatter block, including `tier`, as absent.
FRONTMATTER_RE = re.compile(r"^\ufeff?---\n(.*?)\n---\n*", re.DOTALL)
# Sibling of FRONTMATTER_RE used only by the human-zone extractor (code
# review defect 2): that function's return value must preserve the original
# bytes past the frontmatter block verbatim, including any CRLF line endings
# inside the owner's human zone -- so, unlike every other FRONTMATTER_RE
# caller here (which normalizes CRLF to LF first purely to make the match
# succeed, then only reads matched group text, never slices the input),
# it cannot match against a normalized copy and slice from that copy. This
# pattern tolerates "\r\n" *and* a bare "\r" (classic Mac line endings --
# frontmatter.py::_detect_newline already recognizes this style, and
# patch_frontmatter_bytes preserves it on point-edits, so a page saved once
# in this format stays in it) directly, so it can match the real,
# un-normalized text and hand back a slice of that same text. The
# alternation order matters: "\r\n" must be tried before the bare "\r"
# branch, or a CRLF input would match only its "\r" half and leave the "\n"
# dangling in the slice.
_FRONTMATTER_BOUNDARY_RE = re.compile(
    r"^\ufeff?---(?:\r\n|\r|\n).*?(?:\r\n|\r|\n)---(?:\r\n|\r|\n)*", re.DOTALL
)
_NON_FLAT_HEADER_LINE_RE = re.compile(r"^(?:[ \t]|[-#])", re.MULTILINE)
# Header of a YAML block scalar: `>`/`|`, optional chomping sign and indent digit.
_BLOCK_SCALAR_INDICATOR_RE = re.compile(r"^[>|](?:[+-]?\d?|\d[+-])$")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# ТЗ 4.4: trust is determined by code, never by the model, and is never used
# to pick a conflict winner (only dates/claim kind do that, see
# _effective_conflict_type). The ТЗ states own/integration are strong enough
# for automated consequential actions while forwarded/inferred are not (4.4
# "Правила"), which only fixes a two-tier grouping, not a total order; the
# order within each tier (own > integration, forwarded > inferred) is this
# implementation's own assumption, not stated verbatim in the ТЗ.
TRUST_RANK = {"own": 4, "integration": 3, "forwarded": 2, "inferred": 1}
# Marks a "Sources That Shaped This Page" row added without enrichment (ТЗ
# 6.1: `cold` tier, and a `warm` page whose speculative compile turned out
# insignificant) -- styled like the existing "(not captured)" placeholder so
# it reads the same way in the table. Excluded from the monthly-enrichment
# count (see ``_upsert_briefing``) and from ``enrichment_count`` (see
# ``_record_non_enrichment_source``).
NOT_ENRICHMENT_SOURCE_MARKER = "(not enrichment)"
# Model-authored text that happens to contain one of the human-zone markers
# (HUMAN_ZONE_START/END in compiled_briefings.py) --
# quoted from a forwarded message, echoed back from an injection attempt,
# or simply describing this very mechanism -- must not turn into a second
# marker pair on the page. Unlike daily entries, where a marker only counts
# as one when it owns a whole line (storage.escape_embedded_runtime_markup
# defuses those by indenting), the human zone is found with a plain
# substring count, so indenting would change nothing and the substring
# itself has to be broken. Dropping the "--" after the "<!" does that
# while keeping the text readable and its meaning obvious.
#
# The replacement deliberately adds no character of its own: the callers
# below collapse runs of whitespace *before* defusing, and a claim's text
# is later matched byte for byte against the copy the model was shown
# (see _apply_claims_and_conflicts). Inserting a space here would make
# defusing non-idempotent -- the second cleaning pass would collapse the
# doubled space, the strings would stop matching, and the conflict
# pointing at that claim would be silently dropped as stale.
_HUMAN_ZONE_MARKER_LOOKALIKE_RE = re.compile(r"<!--(\s*human:(?:start|end)\s*)-->")


def defuse_human_zone_markers(text: str) -> str:
    """Break any human-zone marker embedded in model-authored text."""

    return _HUMAN_ZONE_MARKER_LOOKALIKE_RE.sub(r"<!\1-->", text)


def metadata_value(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip('"')
        return str(decoded).strip()
    return raw


def _scan_header_lines(header: str) -> dict[str, str]:
    """Line-by-line ``key: value`` scan; never fails, whatever the header holds."""
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        # Point-edit write paths (see _promote_archive_tier,
        # _record_non_enrichment_source) patch single fields with
        # patch_frontmatter_bytes, which always JSON-quotes string
        # values (e.g. `tier: "warm"`) -- unlike _render_briefing's own
        # plain `f"tier: {tier}"` convention used for the same fields.
        # Decode through ``metadata_value`` -- the one place that
        # unescapes a JSON-quoted value -- instead of naively stripping
        # one wrapping pair of quotes: a naive strip leaves any escaped
        # inner quote untouched, so a value like `"Иван \"Ваня\"
        # Петров"` comes out with the backslashes still in it, and each
        # later json.dumps re-escapes what is already escaped. Every
        # reader of this dict (and every direct caller of
        # ``metadata_value`` that later re-wraps one of these values)
        # sees the same fully-decoded value regardless of which write
        # path produced it.
        fields[key.strip()] = metadata_value(value)
    return fields


def frontmatter_fields(text: str) -> dict[str, str]:
    """Top-level frontmatter fields of one compiled page as plain strings.

    Never raises: one damaged page must not stop a pass over the others.
    Values are the text as written (``tags: [a, b]`` stays ``"[a, b]"``,
    ``0.80`` stays ``"0.80"``), so callers keep parsing them themselves.
    The YAML loader of ``frontmatter.py`` is consulted only to fix what the
    line scan gets wrong: a block scalar (``description: >-``) is read as its
    real text, and indented lines inside such a block are not mistaken for
    fields. A header the loader rejects falls back to the line scan as is.
    Keys YAML turns into another type (``1:``, ``yes:``, ``on:``) are dropped
    when the YAML is parsed.
    """
    # CRLF line endings (e.g. from Windows' `git config core.autocrlf
    # true`) break FRONTMATTER_RE's literal `\n` right after the
    # opening `---`, same as the other FRONTMATTER_RE callers do --
    # without this every field, including `tier`, would read as absent.
    normalized = (text or "").replace("\r\n", "\n")
    match = FRONTMATTER_RE.match(normalized)
    if match is None:
        return {}
    header = match.group(1)
    fields = _scan_header_lines(header)
    # PyYAML is ~80x slower than the line scan, so it runs only for the cases
    # this fix targets: a block scalar value, or lines that are not plain
    # top-level ``key: value`` (indented, list items, comments) and may have
    # produced false keys. Rarer shapes (quoted keys, unindented multi-line
    # quoted values) keep the line-scan result, as before.
    if not any(
        _BLOCK_SCALAR_INDICATOR_RE.match(value) for value in fields.values()
    ) and not _NON_FLAT_HEADER_LINE_RE.search(header):
        return fields
    try:
        parsed = parse_frontmatter_bytes(normalized.encode("utf-8")).fields
    except (FrontmatterError, UnicodeEncodeError):
        return fields
    result: dict[str, str] = {}
    for key, value in fields.items():
        if key not in parsed:
            continue
        if _BLOCK_SCALAR_INDICATOR_RE.match(value) and isinstance(parsed[key], str):
            value = parsed[key].strip()
        result[key] = value
    return result


def decode_page_bytes(raw: bytes) -> str:
    """Decode one compiled page's bytes the way every reader here does."""

    return raw.decode("utf-8", errors="replace")


def read_page_text(path: Path) -> str:
    """Read one compiled page the way ``_upsert_briefing`` writes it.

    Deliberately not ``read_text``: its universal-newline translation
    turns every "\\r" on the page into "\\n", so the same file parses
    into different sections depending on which reader opened it, and
    the text compares unequal to the bytes on disk -- which is how
    ``_compress_cooled_pages``'s compare-and-swap decides the page
    changed under it and silently skips the page forever. The owner
    only has to write a note with CRLF endings inside their own zone
    for that to happen.
    """

    return decode_page_bytes(path.read_bytes())


def title_from_text(text: str) -> str:
    match = TITLE_RE.search(text or "")
    return str(match.group(1)).strip() if match else ""


def body_without_frontmatter(text: str) -> str:
    # Code review defect 2: the body this returns feeds
    # _extract_human_zone, whose output must survive recompilation
    # byte-for-byte -- so the slice below always comes from the
    # *original* ``text``, never from a CRLF-normalized copy (a
    # normalized copy would silently turn every "\r\n" inside the
    # owner's zone into "\n"). ``_FRONTMATTER_BOUNDARY_RE`` tolerates
    # "\r\n" directly (see frontmatter_fields for why plain "\n"
    # alone isn't enough), so it can match ``text`` as-is with no
    # normalization step at all.
    text = text or ""
    match = _FRONTMATTER_BOUNDARY_RE.match(text)
    if match is None:
        return text
    return text[match.end() :]


def int_value(value: str | None, *, default: int) -> int:
    raw = str(value or "").strip()
    try:
        parsed = int(raw)
    except ValueError:
        if raw:
            logger.warning(
                "Compiled briefing has non-numeric counter value %r; "
                "using default %d",
                raw,
                default,
            )
        return default
    # Counters (enrichment_count, conflicts_open) are never meaningful
    # below zero; clamp instead of letting a corrupted negative value
    # keep counting further away from zero on every write.
    return max(parsed, 0)


def tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "")}


def min_trust(first: str, second: str) -> str:
    """Weaker of two trust levels (page-level trust is the fail-closed
    minimum across all of a page's sources, ТЗ 4.3/4.4)."""
    if TRUST_RANK.get(second, 0) < TRUST_RANK.get(first, 0):
        return second
    return first


def clip(text: str, limit: int) -> str:
    source = str(text or "").strip()
    if limit <= 0 or len(source) <= limit:
        return source
    return source[:limit].rstrip() + "\n...[truncated]"


def slugify(value: str) -> str:
    source = str(value or "").strip().lower()
    translit = source.maketrans(
        {
            "а": "a",
            "б": "b",
            "в": "v",
            "г": "g",
            "д": "d",
            "е": "e",
            "ё": "e",
            "ж": "zh",
            "з": "z",
            "и": "i",
            "й": "y",
            "к": "k",
            "л": "l",
            "м": "m",
            "н": "n",
            "о": "o",
            "п": "p",
            "р": "r",
            "с": "s",
            "т": "t",
            "у": "u",
            "ф": "f",
            "х": "h",
            "ц": "ts",
            "ч": "ch",
            "ш": "sh",
            "щ": "sch",
            "ъ": "",
            "ы": "y",
            "ь": "",
            "э": "e",
            "ю": "yu",
            "я": "ya",
        }
    )
    normalized = source.translate(translit)
    normalized = re.sub(r"[^0-9a-z]+", "-", normalized).strip("-")
    return normalized[:80]


def normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        cleaned = " ".join(str(item or "").split()).strip()
        if cleaned:
            lines.append(defuse_human_zone_markers(cleaned))
    return lines[:7]


def canonical_source_reference(raw: str) -> str:
    is_qmd = raw.startswith("qmd:/")
    if is_qmd:
        raw = re.sub(r"^qmd:/+", "", raw)
        if raw.startswith("core/"):
            raw = raw[len("core/"):]
        for collection, folder in (
            ("youtube", "imports/youtube/notes"),
            ("documents", "imports/documents/notes"),
            ("documents_forwarded", "imports/documents/forwarded"),
        ):
            if raw.startswith(f"{collection}/"):
                raw = folder + raw[len(collection):]
                break
    if raw.startswith("plaud/"):
        raw = "imports/plaud/notes/" + raw[len("plaud/"):]
    return raw


def source_links_from_note(text: str) -> list[str]:
    if not text:
        return []
    match = re.search(
        r"^##\s+Sources\s*$\n([\s\S]*?)(?:^##\s+|\Z)",
        text,
        re.MULTILINE,
    )
    if match is None:
        return []
    return [
        path.strip()
        for path in WIKILINK_RE.findall(match.group(1))
        if path.strip()
    ]


def escape_table_cell(value: str) -> str:
    return str(value or "").replace("|", "\\|")


def unescape_table_cell(value: str) -> str:
    return str(value or "").replace("\\|", "|")


def render_sources_shaped_table(
    rows: list[tuple[str, str, str]],
) -> list[str]:
    rows = [row for row in rows if row[2] != NOT_ENRICHMENT_SOURCE_MARKER]
    if not rows:
        return ["(no sources recorded yet)"]
    lines = ["| Date | Source | What Added |", "| --- | --- | --- |"]
    for date_value, source_value, what_value in rows:
        source_cell = escape_table_cell(source_value)
        what_cell = escape_table_cell(what_value)
        lines.append(f"| {date_value} | [[{source_cell}]] | {what_cell} |")
    return lines


def render_claim_history(rows: list[tuple[str, str, str, str]]) -> list[str]:
    if not rows:
        return ["(no superseded claims yet)"]
    lines = [
        "| Date | Source | Claim | Superseded By |",
        "| --- | --- | --- | --- |",
    ]
    for date_value, source_value, claim_value, superseded_value in rows:
        source_cell = escape_table_cell(source_value)
        claim_cell = escape_table_cell(claim_value)
        superseded_cell = escape_table_cell(superseded_value)
        lines.append(
            f"| {date_value} | [[{source_cell}]] | {claim_cell} | "
            f"[[{superseded_cell}]] |"
        )
    return lines


def render_open_conflicts_table(
    rows: list[tuple[str, str, str, str, str]],
) -> list[str]:
    if not rows:
        return ["(no open conflicts)"]
    lines = [
        "| Date | Existing Claim | Existing Source | New Claim | New Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for date_value, existing_claim, existing_source, new_claim, new_source in rows:
        existing_claim_cell = escape_table_cell(existing_claim)
        existing_source_cell = escape_table_cell(existing_source)
        new_claim_cell = escape_table_cell(new_claim)
        new_source_cell = escape_table_cell(new_source)
        lines.append(
            f"| {date_value} | {existing_claim_cell} | "
            f"[[{existing_source_cell}]] | {new_claim_cell} | "
            f"[[{new_source_cell}]] |"
        )
    return lines


# Section and table-row helpers. Their implementation stays in
# ``CompiledBriefingService``: it is the fail-closed human-zone logic
# (``_human_zone_span`` and the marker-corruption checks), which must not
# drift apart from the code that renders and recompiles pages. These are
# thin forwards, imported lazily so this module never imports
# ``compiled_briefings`` while that module is still importing it.


def _service() -> Any:
    from d_brain.services.compiled_briefings import CompiledBriefingService

    return CompiledBriefingService


def section_text(text: str, heading: str) -> str:
    return str(_service()._section_text(text, heading))


def section_bullets(text: str, heading: str) -> list[str]:
    return list(_service()._section_bullets(text, heading))


def has_section(text: str, heading: str) -> bool:
    return bool(_service()._has_section(text, heading))


def replace_section(text: str, heading: str, new_lines: list[str]) -> str:
    return str(_service()._replace_section(text, heading, new_lines))


def insert_section_before(
    text: str,
    *,
    heading: str,
    before_heading: str,
    new_lines: list[str],
) -> str:
    return str(
        _service()._insert_section_before(
            text,
            heading=heading,
            before_heading=before_heading,
            new_lines=new_lines,
        )
    )


def ensure_claim_history_section(text: str) -> str:
    return str(_service()._ensure_claim_history_section(text))


def claim_history_rows(text: str) -> list[tuple[str, str, str, str]]:
    return list(_service()._claim_history_rows(text))


def open_conflicts_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    return list(_service()._open_conflicts_rows(text))


def sources_shaped_rows(text: str) -> list[tuple[str, str, str]]:
    return list(_service()._sources_shaped_rows(text))
