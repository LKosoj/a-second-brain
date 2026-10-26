"""Vault storage service for saving entries."""

import fcntl
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from d_brain.manifest import (
    VaultManifest,
    load_manifest_for_vault,
)
from d_brain.services.atomic_io import atomic_write_file_bytes
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
    entry_block_digest,
    entry_spans,
    extract_entry_statuses,
    format_entry_status_comments,
    normalize_entry_type,
)
from d_brain.services.frontmatter import (
    FrontmatterDocument,
    FrontmatterError,
    UnsafeVaultPathError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.localization import normalize_language
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import (
    SourceInfo,
    escape_embedded_daily_headers,
    escape_embedded_runtime_markup,
    format_source_markdown,
)
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

logger = logging.getLogger(__name__)

# The source line of an entry saved from Telegram ("> Идентификатор источника:
# `telegram:<chat>:<msg>`"); the ":edit" tail marks a correction entry, so a
# search for the original message never lands on its correction.
_TELEGRAM_REF_RE = re.compile(
    r"^>[^\n`]*`(telegram:-?\d+:\d+(?::edit)?)`[ \t]*\r?$", re.MULTILINE
)
_PREVIEW_LIMIT = 80


class ManagedBlockError(ValueError):
    """Raised when existing managed-block markers are unsafe to modify."""


@dataclass(frozen=True, slots=True)
class DailyEntrySummary:
    """What /undo shows about the entry it offers to remove."""

    time: str
    entry_type: str
    preview: str
    digest: str
    processed: bool


@dataclass(frozen=True, slots=True)
class _EntryBlock:
    """One "## HH:MM [type]" entry: [start, end) is heading plus body."""

    start: int
    heading_end: int
    end: int
    time: str
    entry_type: str


class VaultStorage:
    """Service for storing entries in Obsidian vault."""

    def __init__(self, vault_path: Path, content_language: str = "ru") -> None:
        self.vault_path = Path(vault_path)
        self.content_language = normalize_language(content_language)
        self.daily_path = self.vault_path / "daily"
        self.attachments_path = self.vault_path / "attachments"
        self.locks_path = self.vault_path / ".locks"

    def _ensure_dirs(self) -> None:
        """Ensure required directories exist."""
        self.daily_path.mkdir(parents=True, exist_ok=True)
        self.attachments_path.mkdir(parents=True, exist_ok=True)
        self.locks_path.mkdir(parents=True, exist_ok=True)

    def get_daily_file(self, day: date) -> Path:
        """Get path to daily file for given date."""
        self._ensure_dirs()
        return self.daily_path / f"{day.isoformat()}.md"

    def read_daily(self, day: date) -> str:
        """Read content of daily file under shared lock."""
        file_path = self.get_daily_file(day)
        if not file_path.exists():
            return ""
        with self._daily_lock(day):
            return file_path.read_text(encoding="utf-8")

    @contextmanager
    def _daily_lock(self, day: date) -> Iterator[None]:
        """Serialize writes to one daily file across concurrent runtime paths."""
        self._ensure_dirs()
        lock_path = self.locks_path / f"daily-{day.isoformat()}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _daily_frontmatter(day: date, fields: dict[str, str] | None = None) -> str:
        """Render canonical daily frontmatter with stable field order."""
        merged = dict(fields or {})
        merged["type"] = "daily"
        merged["date"] = day.isoformat()
        merged.setdefault("last_accessed", day.isoformat())
        merged.setdefault("relevance", "1.0")
        merged.setdefault("tier", "active")

        ordered_keys = ("type", "date", "last_accessed", "relevance", "tier")
        lines = ["---"]
        for key in ordered_keys:
            lines.append(f"{key}: {merged.pop(key)}")
        for key in sorted(merged):
            value = str(merged[key]).strip()
            if value:
                lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    @classmethod
    def _bootstrap_daily_content(cls, day: date, current: str) -> str:
        """Bootstrap daily structure without reserializing valid frontmatter."""
        expected_heading = f"# {day.isoformat()}"
        if not current.strip():
            return f"{cls._daily_frontmatter(day)}\n\n{expected_heading}\n"

        source = current.encode("utf-8")
        try:
            document = parse_frontmatter_bytes(source)
        except FrontmatterError:
            # A single malformed block (duplicate key, a tab, an unquoted
            # colon) is a corrupted file, not a legacy layout -- repairing
            # it here would silently drop whatever the strict parser could
            # not read (a YAML list under ``tags:``, a nested mapping) and
            # leave every message for the day. Only the specific doubled
            # legacy shape below is safe to rewrite; anything else must
            # surface to the caller the same way any other write failure
            # does (Telegram handlers, the plaud sync loop and the daily
            # cycle all already convert an uncaught ValueError into a
            # user-visible error rather than crashing).
            if not cls._is_doubled_legacy_frontmatter(current, expected_heading):
                raise
            return cls._repair_legacy_daily_content(day, current)

        if document.has_frontmatter:
            updates: dict[str, object] = {}
            if document.fields.get("type") != "daily":
                updates["type"] = "daily"
            if document.fields.get("date") != day.isoformat():
                updates["date"] = day.isoformat()
            defaults: dict[str, object] = {
                "last_accessed": day.isoformat(),
                "relevance": 1.0,
                "tier": "active",
            }
            updates.update(
                {
                    key: value
                    for key, value in defaults.items()
                    if document.fields.get(key) in (None, "", [])
                }
            )
            patched = patch_frontmatter_bytes(source, updates)
            return cls._ensure_daily_heading(day, patched)

        legacy_heading_then_frontmatter = re.match(
            rf"\A\s*{re.escape(expected_heading)}\s*\n+\s*---\s*\n",
            current,
        )
        if legacy_heading_then_frontmatter is not None:
            return cls._repair_legacy_daily_content(day, current)

        if re.match(
            rf"\A(?:[ \t]*\r?\n)*{re.escape(expected_heading)}(?:\r?\n|\Z)",
            current,
        ):
            return f"{cls._daily_frontmatter(day)}\n\n{current}"
        return (
            f"{cls._daily_frontmatter(day)}\n\n"
            f"{expected_heading}\n\n"
            f"{current}"
        )

    @classmethod
    def _ensure_daily_heading(cls, day: date, content: bytes) -> str:
        """Insert a missing daily H1 without changing existing body bytes."""
        document = parse_frontmatter_bytes(content)
        expected_heading = f"# {day.isoformat()}"
        body = document.body.decode("utf-8")
        if re.match(
            rf"\A(?:[ \t]*\r?\n)*{re.escape(expected_heading)}(?:\r?\n|\Z)",
            body,
        ):
            return content.decode("utf-8")

        prefix_length = len(content) - len(document.body)
        prefix = content[:prefix_length]
        newline = document.newline
        insertion = newline + expected_heading.encode("utf-8") + newline
        if document.body:
            insertion += newline
        return (prefix + insertion + document.body).decode("utf-8")

    @staticmethod
    def _leading_legacy_block_count(current: str, expected_heading: str) -> int:
        """Count ``---``-delimited blocks stacked at the top of ``current``.

        Shared by the doubled-frontmatter detector and the repair below, so
        both agree on what counts as one block: a standalone heading line is
        skipped, not counted, matching the loop's own tolerance for a stray
        heading sitting between two frontmatter blocks.
        """
        working = current.replace("\r\n", "\n").replace("\r", "\n").lstrip()
        frontmatter_re = re.compile(r"\A---\n(.*?)\n---\n*", re.DOTALL)
        blocks = 0
        while working:
            match = frontmatter_re.match(working)
            if match is not None:
                blocks += 1
                working = working[match.end() :].lstrip()
                continue
            line, _, rest = working.partition("\n")
            if line.strip() == expected_heading:
                working = rest.lstrip()
                continue
            break
        return blocks

    @classmethod
    def _is_doubled_legacy_frontmatter(
        cls, current: str, expected_heading: str
    ) -> bool:
        """Detect the doubled-frontmatter shape the repair below unwinds.

        A single malformed block (duplicate key, tabs, an unquoted colon) is
        corruption, not this legacy shape -- it always counts as one block
        here, so it is never mistaken for the two-or-more-blocks case a past
        writer bug is known to have produced.
        """
        return cls._leading_legacy_block_count(current, expected_heading) >= 2

    @classmethod
    def _repair_legacy_daily_content(cls, day: date, current: str) -> str:
        """Repair explicitly malformed legacy daily preamble layouts."""
        expected_heading = f"# {day.isoformat()}"
        newline = b"\r\n" if "\r\n" in current else b"\n"
        remaining = current.replace("\r\n", "\n").replace("\r", "\n").lstrip()
        merged_fields: dict[str, str] = {}
        frontmatter_re = re.compile(r"\A---\n(.*?)\n---\n*", re.DOTALL)

        while remaining:
            frontmatter_match = frontmatter_re.match(remaining)
            if frontmatter_match is not None:
                for raw_line in frontmatter_match.group(1).splitlines():
                    if ":" not in raw_line:
                        continue
                    key, _, value = raw_line.partition(":")
                    cleaned_key = key.strip()
                    cleaned_value = value.strip()
                    if cleaned_key:
                        merged_fields[cleaned_key] = cleaned_value
                remaining = remaining[frontmatter_match.end() :].lstrip()
                continue

            line, _, rest = remaining.partition("\n")
            if line.strip() == expected_heading:
                remaining = rest.lstrip()
                continue
            break

        body = remaining.lstrip()
        preamble = f"{cls._daily_frontmatter(day, merged_fields)}\n\n{expected_heading}"
        if not body:
            repaired = preamble + "\n"
        else:
            repaired = f"{preamble}\n\n{body.rstrip()}\n"
        return cls._render_with_newline(repaired, newline).decode("utf-8")

    def _post_daily_write(
        self,
        file_path: Path,
        *,
        refresh_qmd: bool,
        refresh_compiled: bool,
        source_excerpt: str = "",
    ) -> None:
        """Keep secondary indexes in sync after one markdown write.

        Every step here is a *secondary* index: the daily write itself has
        already committed to disk by the time this runs. A failure while
        re-indexing therefore deserves a log line, never an exception thrown
        back at the caller -- the document handler, for one, wraps its whole
        pipeline in a single try/except and would answer "❌ Не удалось
        обработать документ" about a document it had in fact saved, sending
        the owner looking for a file that is already there.
        """
        try:
            DailyEntryMemoryStore(self.vault_path).sync_daily_file(file_path)
        except Exception:
            logger.exception("Daily entry memory sync failed: %s", file_path)
        if refresh_compiled:
            try:
                self._refresh_compiled_briefings(
                    file_path, source_excerpt=source_excerpt
                )
            except Exception:
                logger.exception("Compiled briefing refresh failed: %s", file_path)
        if refresh_qmd:
            try:
                self._refresh_qmd_index()
            except Exception:
                logger.exception("qmd refresh failed after writing %s", file_path)

    def _manifest(self) -> VaultManifest:
        """Load the required project manifest before every daily write."""
        return load_manifest_for_vault(self.vault_path)

    def _write_daily_markdown(
        self,
        file_path: Path,
        content: str | bytes,
        lock: VaultWriteLock,
    ) -> None:
        """Persist one daily profile under the shared vault writer lock."""
        payload = content.encode("utf-8") if isinstance(content, str) else content
        write_validated_vault_markdown(
            self.vault_path,
            file_path,
            payload,
            manifest=self._manifest(),
            existing_lock=lock,
        )

    def ensure_daily_file(self, day: date) -> Path:
        """Ensure a daily file exists with the canonical header."""
        self._ensure_dirs()
        file_path = self.get_daily_file(day)
        with vault_write_lock(self.vault_path) as lock:
            with self._daily_lock(day):
                if file_path.exists():
                    current = file_path.read_bytes().decode("utf-8")
                else:
                    current = ""
                content = self._bootstrap_daily_content(day, current)
                if content != current:
                    self._write_daily_markdown(file_path, content, lock)
        return file_path

    @staticmethod
    def _managed_block_bounds(
        document: FrontmatterDocument,
        *,
        start_marker: str,
        end_marker: str,
    ) -> tuple[int, int] | None:
        """Locate one unambiguous standalone marker pair in the Markdown body."""
        start_marker_bytes = start_marker.encode("utf-8")
        end_marker_bytes = end_marker.encode("utf-8")
        body = document.body
        body_offset = len(document.content) - len(body)
        starts: list[int] = []
        ends: list[tuple[int, int]] = []

        line_start = 0
        while line_start < len(body):
            newline_at = body.find(b"\n", line_start)
            if newline_at == -1:
                line_end = len(body)
                next_line = len(body)
            else:
                line_end = newline_at
                next_line = newline_at + 1
                if line_end > line_start and body[line_end - 1 : line_end] == b"\r":
                    line_end -= 1

            line = body[line_start:line_end]
            if line == start_marker_bytes:
                starts.append(line_start)
            if line == end_marker_bytes:
                ends.append((line_start, next_line))
            line_start = next_line

        if not starts and not ends:
            return None
        if len(starts) != 1 or len(ends) != 1:
            raise ManagedBlockError(
                "managed block requires exactly one standalone marker pair: "
                f"start={len(starts)} end={len(ends)}"
            )

        start = starts[0]
        end, end_line = ends[0]
        if end <= start:
            raise ManagedBlockError(
                "managed block end marker must follow its start marker"
            )
        return (body_offset + start, body_offset + end_line)

    @staticmethod
    def _trailing_line_break_count(content: bytes, *, limit: int = 2) -> int:
        """Count up to ``limit`` trailing CRLF/LF/CR line breaks."""
        offset = len(content)
        count = 0
        while offset > 0 and count < limit:
            if offset >= 2 and content[offset - 2 : offset] == b"\r\n":
                offset -= 2
            elif content[offset - 1 : offset] in {b"\n", b"\r"}:
                offset -= 1
            else:
                break
            count += 1
        return count

    @classmethod
    def _minimal_block_separator(cls, content: bytes, newline: bytes) -> bytes:
        """Add only the line breaks missing from a two-break block boundary."""
        missing = 2 - cls._trailing_line_break_count(content)
        return newline * missing

    @staticmethod
    def _render_with_newline(content: str, newline: bytes) -> bytes:
        """Render newly generated text with the document's newline convention."""
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.replace("\n", newline.decode("ascii")).encode("utf-8")

    @classmethod
    def _render_managed_block(cls, block: str, newline: bytes) -> bytes:
        """Normalize a caller block while leaving surrounding document bytes alone."""
        rendered = cls._render_with_newline(block, newline)
        while rendered.startswith(newline):
            rendered = rendered[len(newline) :]
        return rendered

    def append_to_daily(
        self,
        text: str,
        timestamp: datetime,
        msg_type: str,
        *,
        entry_statuses: list[str] | tuple[str, ...] | None = None,
        source: SourceInfo | None = None,
        refresh_qmd: bool = True,
    ) -> None:
        """Append entry to daily file.

        Args:
            text: Content to append
            timestamp: Entry timestamp
            msg_type: Type marker like [voice], [text], [photo], [forward from: Name]
        """
        self._ensure_dirs()
        day = timestamp.date()
        file_path = self.get_daily_file(day)

        time_str = timestamp.strftime("%H:%M")
        status_block = format_entry_status_comments(entry_statuses or ())
        source_block = format_source_markdown(
            source,
            language=self.content_language,
        )
        # A body line shaped like "## HH:MM [...]" would be read back as a
        # second, independent daily entry -- with its own, possibly
        # stronger, trust rating -- once this file is split back into
        # entries (see escape_embedded_daily_headers for why this runs for
        # every entry, not only forwarded ones).
        # Runtime markup gets the same treatment as a header lookalike: this
        # body is someone else's words often enough (a forwarded message,
        # extracted document text, image OCR) and the runtime's marker
        # strings are public, so a body line can otherwise host the reflect
        # block, wedge every later write for the day, or forge a status.
        body = escape_embedded_runtime_markup(
            escape_embedded_daily_headers(text.strip())
        )
        entry_parts = [part for part in (status_block, source_block, body) if part]
        entry_body = "\n\n".join(entry_parts)
        # Keep the "## HH:MM [type]" header attached to the body: the
        # compiled-briefings pipeline reads the entry marker off the first
        # line of the excerpt to tell an own entry from a forwarded one, and
        # a body-only excerpt makes every immediate refresh fall back to the
        # weakest provenance level.
        entry_markdown = f"## {time_str} {msg_type}\n{entry_body}"

        with vault_write_lock(self.vault_path) as lock:
            with self._daily_lock(day):
                if file_path.exists():
                    current = file_path.read_bytes().decode("utf-8")
                else:
                    current = ""
                content = self._bootstrap_daily_content(day, current)
                content_bytes = content.encode("utf-8")
                document = parse_frontmatter_bytes(content_bytes)
                entry = self._render_with_newline(
                    f"{entry_markdown}\n",
                    document.newline,
                )
                updated = (
                    content_bytes
                    + self._minimal_block_separator(
                        content_bytes,
                        document.newline,
                    )
                    + entry
                )
                self._write_daily_markdown(file_path, updated, lock)
        self._post_daily_write(
            file_path,
            refresh_qmd=refresh_qmd,
            refresh_compiled=True,
            source_excerpt=entry_markdown,
        )

    def upsert_daily_block(
        self,
        *,
        day: date,
        start_marker: str,
        end_marker: str,
        block: str,
        refresh_qmd: bool = True,
    ) -> None:
        """Insert a full block, or replace only its exact marker-owned range."""
        self._ensure_dirs()
        # Same reason as in append_to_daily: a block line shaped like
        # "## HH:MM [...]" reads back as its own daily entry, and this path
        # carries other people's words too -- PLAUD meeting summaries and
        # the reflect block's AI-written task and note titles come through
        # here. The reflect block is the one that needs it: it heads itself
        # "[text]", so _daily_source_chunks' floor rates a forged header
        # below it "own". Callers pass heading and payload as one string,
        # so keep_leading_header spares the heading they built themselves.
        block = escape_embedded_daily_headers(block, keep_leading_header=True)
        file_path = self.get_daily_file(day)

        with vault_write_lock(self.vault_path) as lock:
            with self._daily_lock(day):
                if file_path.exists():
                    current = file_path.read_bytes().decode("utf-8")
                else:
                    current = ""
                content = self._bootstrap_daily_content(day, current)
                content_bytes = content.encode("utf-8")
                document = parse_frontmatter_bytes(content_bytes)
                bounds = self._managed_block_bounds(
                    document,
                    start_marker=start_marker,
                    end_marker=end_marker,
                )
                rendered_block = self._render_managed_block(
                    block,
                    document.newline,
                )
                rendered_document = parse_frontmatter_bytes(rendered_block)
                rendered_bounds = self._managed_block_bounds(
                    rendered_document,
                    start_marker=start_marker,
                    end_marker=end_marker,
                )
                if rendered_bounds is None:
                    raise ManagedBlockError(
                        "managed block payload contains no standalone marker pair"
                    )
                if bounds is not None:
                    start, end = bounds
                    rendered_start, rendered_end = rendered_bounds
                    replacement = rendered_block[rendered_start:rendered_end]
                    suffix = content_bytes[end:]
                    if (
                        suffix
                        and self._trailing_line_break_count(replacement) == 0
                    ):
                        replacement += document.newline
                    updated = (
                        content_bytes[:start]
                        + replacement
                        + suffix
                    )
                else:
                    updated = (
                        content_bytes
                        + self._minimal_block_separator(
                            content_bytes,
                            document.newline,
                        )
                        + rendered_block
                    )
                self._write_daily_markdown(file_path, updated, lock)
        self._post_daily_write(
            file_path,
            refresh_qmd=refresh_qmd,
            refresh_compiled=False,
        )

    @staticmethod
    def _entry_blocks(content: str) -> list[_EntryBlock]:
        """Split a daily file into entries; offsets index ``content`` as is."""
        return [
            _EntryBlock(
                start=match.start(),
                heading_end=match.end(),
                end=end,
                time=match.group("time"),
                entry_type=normalize_entry_type(match.group("type")),
            )
            for match, end in entry_spans(content)
        ]

    @classmethod
    def _find_telegram_entry(
        cls, content: str, source_ref: str, *, first_ref_only: bool
    ) -> _EntryBlock | None:
        """Last entry carrying ``source_ref``; the managed [d-brain] block never.

        A text entry has exactly one genuine source line, right under its
        heading, so only that one counts there: a ref line quoted further
        down in someone else's words must not redirect an edit. An album
        holds one source line per photo, hence ``first_ref_only=False``.
        """
        for block in reversed(cls._entry_blocks(content)):
            if block.entry_type == "d-brain":
                continue
            refs = [
                match.group(1)
                for match in _TELEGRAM_REF_RE.finditer(
                    content, block.heading_end, block.end
                )
            ]
            if first_ref_only:
                refs = refs[:1]
            if source_ref in refs:
                return block
        return None

    @staticmethod
    def _is_processed(block_text: str) -> bool:
        return ENTRY_STATUS_ALREADY_PROCESSED in extract_entry_statuses(block_text)

    def _replace_entry_body(
        self, old_block: str, body: str, newline: bytes
    ) -> str:
        """Keep heading, status comments and source line; swap everything below."""
        ref_match = _TELEGRAM_REF_RE.search(old_block)
        if ref_match is None:
            return old_block
        trailing = old_block[len(old_block.rstrip()) :]
        rendered = self._render_with_newline(body, newline).decode("utf-8")
        nl = newline.decode("ascii")
        return f"{old_block[: ref_match.end()]}{nl}{nl}{rendered}{trailing}"

    def _replace_caption(
        self, old_block: str, old_caption: str, caption: str, newline: bytes
    ) -> str | None:
        """Swap the old caption for the new one, but only as a whole line.

        A bare substring match could hit the attachment path or the AI
        description that happens to contain the same words.
        """
        old = old_caption.strip()
        if not old:
            return None
        position = old_block.find(old)
        while position != -1:
            end = position + len(old)
            if (position == 0 or old_block[position - 1] == "\n") and (
                end == len(old_block) or old_block[end] in "\r\n"
            ):
                rendered = self._render_with_newline(caption, newline).decode("utf-8")
                return old_block[:position] + rendered + old_block[end:]
            position = old_block.find(old, position + 1)
        return None

    def edit_daily_entry(
        self,
        day: date,
        source_ref: str,
        new_text: str,
        *,
        old_caption: str | None = None,
    ) -> str:
        """Rewrite one saved Telegram entry after the owner edited the message.

        Returns "replaced", "unchanged", "processed" (the nightly pass has
        already read the entry, so it is left alone), "no_caption" (the photo
        entry exists but had no caption to replace) or "missing" (no entry
        with this ref, or the old caption is not in it). Search, status check
        and write happen under the same locks, so a parallel append or the
        nightly marking cannot slip in between. ``old_caption`` switches from
        replacing the entry text to replacing a photo caption.
        """
        file_path = self.get_daily_file(day)
        if not file_path.exists():
            return "missing"
        body = escape_embedded_runtime_markup(
            escape_embedded_daily_headers(new_text.strip())
        )
        with vault_write_lock(self.vault_path) as lock:
            with self._daily_lock(day):
                content = file_path.read_bytes().decode("utf-8")
                block = self._find_telegram_entry(
                    content, source_ref, first_ref_only=old_caption is None
                )
                if block is None:
                    return "missing"
                old_block = content[block.start : block.end]
                if self._is_processed(old_block):
                    return "processed"
                newline = parse_frontmatter_bytes(content.encode("utf-8")).newline
                if old_caption is None:
                    new_block = self._replace_entry_body(old_block, body, newline)
                elif not old_caption.strip():
                    return "no_caption"
                else:
                    replaced = self._replace_caption(
                        old_block, old_caption, body, newline
                    )
                    if replaced is None:
                        return "missing"
                    new_block = replaced
                if new_block == old_block:
                    return "unchanged"
                updated = content[: block.start] + new_block + content[block.end :]
                self._write_daily_markdown(file_path, updated, lock)
        self._post_daily_write(
            file_path,
            refresh_qmd=True,
            refresh_compiled=True,
            source_excerpt=new_block.replace("\r\n", "\n").strip(),
        )
        return "replaced"

    def last_undoable_entry(self, day: date) -> DailyEntrySummary | None:
        """The newest entry saved from Telegram that day, for /undo.

        Entries without a Telegram ref (PLAUD, the managed [d-brain] block)
        are skipped: /undo only takes back what the owner just sent.
        """
        file_path = self.get_daily_file(day)
        if not file_path.exists():
            return None
        with self._daily_lock(day):
            content = file_path.read_bytes().decode("utf-8")
        for block in reversed(self._entry_blocks(content)):
            if block.entry_type == "d-brain":
                continue
            ref_match = _TELEGRAM_REF_RE.search(content, block.heading_end, block.end)
            if ref_match is None:
                continue
            body = content[block.heading_end : block.end]
            preview = " ".join(content[ref_match.end() : block.end].split())
            if len(preview) > _PREVIEW_LIMIT:
                preview = preview[: _PREVIEW_LIMIT - 1] + "…"
            return DailyEntrySummary(
                time=block.time,
                entry_type=block.entry_type,
                preview=preview,
                digest=entry_block_digest(body),
                processed=self._is_processed(body),
            )
        return None

    def entry_ref_by_digest(self, day: date, digest: str) -> str | None:
        """Telegram ref of the entry with this body fingerprint, if still there."""
        file_path = self.get_daily_file(day)
        if not file_path.exists():
            return None
        with self._daily_lock(day):
            content = file_path.read_bytes().decode("utf-8")
        for block in self._entry_blocks(content):
            if block.entry_type == "d-brain":
                continue
            if entry_block_digest(content[block.heading_end : block.end]) != digest:
                continue
            ref_match = _TELEGRAM_REF_RE.search(content, block.heading_end, block.end)
            if ref_match is not None:
                return ref_match.group(1)
        return None

    def delete_daily_entry(self, day: date, digest: str) -> str:
        """Remove the Telegram entry whose body fingerprint is ``digest``.

        Returns "deleted", "processed" or "missing". The fingerprint doubles as
        a compare-and-swap: if the entry was edited after /undo showed it, the
        digest no longer matches and nothing is removed. Attachment files stay.
        """
        file_path = self.get_daily_file(day)
        if not file_path.exists():
            return "missing"
        with vault_write_lock(self.vault_path) as lock:
            with self._daily_lock(day):
                content = file_path.read_bytes().decode("utf-8")
                for block in self._entry_blocks(content):
                    body = content[block.heading_end : block.end]
                    if (
                        block.entry_type == "d-brain"
                        or entry_block_digest(body) != digest
                        or _TELEGRAM_REF_RE.search(body) is None
                    ):
                        continue
                    if self._is_processed(body):
                        return "processed"
                    before = content[: block.start]
                    after = content[block.end :]
                    if not after:
                        newline = parse_frontmatter_bytes(
                            content.encode("utf-8")
                        ).newline.decode("ascii")
                        before = before.rstrip("\r\n") + newline
                    self._write_daily_markdown(file_path, before + after, lock)
                    break
                else:
                    return "missing"
        self._post_daily_write(file_path, refresh_qmd=True, refresh_compiled=False)
        return "deleted"

    def _refresh_compiled_briefings(
        self,
        file_path: Path,
        *,
        source_excerpt: str = "",
    ) -> None:
        """Best-effort queue enqueue for compiled briefing refresh."""
        try:
            service = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
            )
            result = service.enqueue_refresh(
                source_path=file_path,
                source_excerpt=source_excerpt,
            )
            service.spawn_background_drain()
        except Exception as exc:
            logger.warning("compiled refresh skipped/failed for %s: %s", file_path, exc)
            return
        if result["errors"] and result["errors"] not in (
            ["ai-cli-unavailable"],
            ["empty-source"],
            ["unsupported-path"],
        ):
            logger.warning(
                "compiled refresh skipped/failed for %s: %s",
                file_path,
                " | ".join(result["errors"]),
            )

    def _refresh_qmd_index(self) -> None:
        """Keep qmd current after Python-controlled memory writes."""
        result = QmdService(self.vault_path).refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    def get_attachments_dir(self, day: date) -> Path:
        """Get attachments directory for given date."""
        dir_path = self.attachments_path / day.isoformat()
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path

    def save_attachment(
        self,
        data: bytes,
        day: date,
        timestamp: datetime,
        extension: str = "jpg",
        name_hint: str | None = None,
    ) -> str:
        """Save attachment and return relative path for Obsidian embed.

        Args:
            data: File bytes
            day: Date for organizing
            timestamp: Timestamp for filename
            extension: File extension

        Returns:
            Relative path for Obsidian embed: attachments/YYYY-MM-DD/img-HHMMSS.ext
        """
        dir_path = self.get_attachments_dir(day)
        # No vault lock here on purpose: the bot must not wait for the nightly
        # run just to store a photo. Names are unique (``require_absent``), so
        # the only thing to guard is a symlinked directory leading outside.
        for directory in (self.attachments_path, dir_path):
            if directory.is_symlink():
                raise UnsafeVaultPathError(
                    f"attachments directory is a symlink: {directory}"
                )
        time_str = timestamp.strftime("%H%M%S")
        suffix = ""
        if name_hint:
            safe_hint = re.sub(r"[^0-9A-Za-z_-]+", "-", name_hint).strip("-")
            if safe_hint:
                suffix = f"-{safe_hint}"
        base = f"img-{time_str}{suffix}"
        filename = f"{base}.{extension}"
        file_path = dir_path / filename
        counter = 1
        while True:
            # ``lexists``: a dangling symlink also occupies the name.
            if not os.path.lexists(file_path):
                try:
                    atomic_write_file_bytes(file_path, data, require_absent=True)
                    break
                except FileExistsError:
                    pass
            filename = f"{base}-{counter}.{extension}"
            file_path = dir_path / filename
            counter += 1

        return f"attachments/{day.isoformat()}/{filename}"
