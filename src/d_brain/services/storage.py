"""Vault storage service for saving entries."""

import fcntl
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from d_brain.manifest import (
    VaultManifest,
    load_manifest_for_vault,
)
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.entry_status import format_entry_status_comments
from d_brain.services.frontmatter import (
    FrontmatterDocument,
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.localization import normalize_language
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import SourceInfo, format_source_markdown
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

logger = logging.getLogger(__name__)


class ManagedBlockError(ValueError):
    """Raised when existing managed-block markers are unsafe to modify."""


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

    @classmethod
    def _repair_legacy_daily_content(cls, day: date, current: str) -> str:
        """Repair explicitly malformed legacy daily preamble layouts."""
        expected_heading = f"# {day.isoformat()}"
        remaining = current.lstrip()
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
            return preamble + "\n"
        return f"{preamble}\n\n{body.rstrip()}\n"

    def _post_daily_write(
        self,
        file_path: Path,
        *,
        refresh_qmd: bool,
        refresh_compiled: bool,
        source_excerpt: str = "",
    ) -> None:
        """Keep secondary indexes in sync after one markdown write."""
        DailyEntryMemoryStore(self.vault_path).sync_daily_file(file_path)
        if refresh_compiled:
            self._refresh_compiled_briefings(file_path, source_excerpt=source_excerpt)
        if refresh_qmd:
            self._refresh_qmd_index()

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
        body = text.strip()
        entry_parts = [part for part in (status_block, source_block, body) if part]
        entry_body = "\n\n".join(entry_parts)

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
                    f"## {time_str} {msg_type}\n{entry_body}\n",
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
            source_excerpt=entry_body,
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
        while file_path.exists():
            filename = f"{base}-{counter}.{extension}"
            file_path = dir_path / filename
            counter += 1

        file_path.write_bytes(data)

        return f"attachments/{day.isoformat()}/{filename}"
