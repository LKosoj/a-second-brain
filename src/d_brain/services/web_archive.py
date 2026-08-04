"""Immutable archive artifacts for generic web sources."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from d_brain.control_plane.contracts import WorkflowSpec
from d_brain.control_plane.registry import get_workflow
from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.frontmatter import (
    require_safe_vault_root,
    write_import_vault_markdown,
    write_vault_file_text,
)
from d_brain.services.localization import normalize_language, translate
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import SourceInfo, format_source_markdown
from d_brain.services.web_content import WebContentResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebArchiveResult:
    """Persisted artifacts for one fetched web page."""

    raw_path: str
    content_path: str
    note_path: str
    daily_content: str
    content_sha256: str


class WebArchiveService:
    """Persist an immutable extracted page snapshot and searchable note."""

    CONTROL_PLANE_WORKFLOW = "integration.web.archive"

    def __init__(
        self,
        vault_path: Path | str,
        *,
        content_language: str = "ru",
        ai_cli: str = "claude",
    ) -> None:
        self.vault_path = Path(vault_path).absolute()
        self.content_language = normalize_language(content_language)
        self.ai_cli = ai_cli
        self._manifest: VaultManifest | None = None
        self.web_root = self.vault_path / "imports" / "web"
        self.raw_root = self.web_root / "raw"
        self.content_root = self.web_root / "content"
        self.notes_root = self.web_root / "notes"
        self._workflow = self._load_control_plane_workflow()

    @classmethod
    def _load_control_plane_workflow(cls) -> WorkflowSpec:
        workflow = get_workflow(cls.CONTROL_PLANE_WORKFLOW)
        expected_entrypoint = f"{cls.__module__}.{cls.__name__}.archive_page"
        if workflow.entrypoint != expected_entrypoint:
            raise ValueError(
                "Control-plane registry drift for web archive: "
                f"expected {expected_entrypoint}, got {workflow.entrypoint}"
            )
        return workflow

    @property
    def workflow_name(self) -> str:
        """Canonical control-plane workflow name for this service."""
        return self._workflow.name

    def _manifest_for_writes(self) -> VaultManifest:
        require_safe_vault_root(self.vault_path)
        if self._manifest is None:
            self._manifest = load_manifest_for_vault(self.vault_path)
        return self._manifest

    def archive_page(
        self,
        page: WebContentResult,
        *,
        original_url: str,
        timestamp: datetime,
        summary: str = "",
        source: SourceInfo | None = None,
        refresh_qmd: bool = True,
    ) -> WebArchiveResult:
        """Write one immutable source snapshot plus its searchable summary note."""
        manifest = self._manifest_for_writes()

        content = page.content.strip()
        if not content:
            raise ValueError("Cannot archive an empty web source")

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stem = self._source_stem(page.url or original_url, digest)
        raw_path = self._build_path(
            root=self.raw_root,
            timestamp=timestamp,
            stem=stem,
            suffix=".json",
            nested_by_month=False,
        )
        content_path = self._build_path(
            root=self.content_root,
            timestamp=timestamp,
            stem=stem,
            suffix=".txt",
            nested_by_month=False,
        )
        note_path = self._build_path(
            root=self.notes_root,
            timestamp=timestamp,
            stem=stem,
            suffix=".md",
            nested_by_month=True,
        )
        raw_payload = {
            "schema_version": 1,
            "original_url": original_url,
            "final_url": page.url,
            "title": page.title,
            "extractor": page.source,
            "captured_at": timestamp.isoformat(),
            "content_sha256": digest,
            "content": content,
        }
        write_vault_file_text(
            self.vault_path,
            raw_path,
            json.dumps(raw_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            require_absent=True,
        )
        write_vault_file_text(
            self.vault_path,
            content_path,
            content + "\n",
            require_absent=True,
        )

        raw_rel = raw_path.relative_to(self.vault_path).as_posix()
        content_rel = content_path.relative_to(self.vault_path).as_posix()
        note_rel = note_path.relative_to(self.vault_path).as_posix()
        write_import_vault_markdown(
            self.vault_path,
            note_path,
            self._build_note(
                page=page,
                original_url=original_url,
                timestamp=timestamp,
                summary=summary,
                source=source,
                raw_path=raw_rel,
                content_path=content_rel,
                content_sha256=digest,
            ),
            manifest=manifest,
        )
        self._refresh_compiled_briefings(
            note_path=note_rel,
            source_excerpt="\n\n".join(
                part for part in (summary.strip(), content[:3500]) if part
            ),
        )
        if refresh_qmd:
            self._refresh_qmd_index()

        return WebArchiveResult(
            raw_path=raw_rel,
            content_path=content_rel,
            note_path=note_rel,
            daily_content=self._build_daily_content(
                page=page,
                original_url=original_url,
                summary=summary,
                note_path=note_rel,
                raw_path=raw_rel,
                content_path=content_rel,
            ),
            content_sha256=digest,
        )

    def _build_note(
        self,
        *,
        page: WebContentResult,
        original_url: str,
        timestamp: datetime,
        summary: str,
        source: SourceInfo | None,
        raw_path: str,
        content_path: str,
        content_sha256: str,
    ) -> str:
        title = page.title.strip() or translate(
            self.content_language, "unknown_title"
        )
        description = (
            " ".join(summary.strip().split()) if summary.strip() else title
        )[:240]
        source_block = format_source_markdown(source, language=self.content_language)
        parts = [
            "---",
            "type: web-source",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            "tags: [web, source]",
            "status: active",
            f"source_url: {json.dumps(original_url, ensure_ascii=False)}",
            f"final_url: {json.dumps(page.url, ensure_ascii=False)}",
            f"extractor: {json.dumps(page.source, ensure_ascii=False)}",
            f"captured_at: {timestamp.isoformat()}",
            f"content_sha256: {content_sha256}",
            f"raw_path: {raw_path}",
            f"content_path: {content_path}",
            f"created: {timestamp.date().isoformat()}",
            f"updated: {timestamp.date().isoformat()}",
            "---",
            "",
            f"# {title}",
        ]
        if source_block:
            parts.extend(["", source_block])
        parts.extend(
            [
                "",
                f"## {translate(self.content_language, 'summary')}",
                summary.strip()
                or translate(self.content_language, "summary_unavailable"),
                "",
                f"## {translate(self.content_language, 'source')}",
                f"- URL: {original_url}",
                f"- Final URL: {page.url}",
                f"- Extractor: `{page.source}`",
                f"- {translate(self.content_language, 'raw_payload')}: "
                f"[[{raw_path}]]",
                f"- {translate(self.content_language, 'extracted_text')}: "
                f"[[{content_path}]]",
                f"- SHA-256: `{content_sha256}`",
            ]
        )
        return "\n".join(parts).rstrip() + "\n"

    def _build_daily_content(
        self,
        *,
        page: WebContentResult,
        original_url: str,
        summary: str,
        note_path: str,
        raw_path: str,
        content_path: str,
    ) -> str:
        title = page.title.strip() or translate(
            self.content_language, "unknown_title"
        )
        parts = [
            f"🌐 [[{note_path}|{title}]]",
            f"> URL: {original_url}",
            f"> {translate(self.content_language, 'extracted_text')}: "
            f"[[{content_path}]]",
            f"> {translate(self.content_language, 'raw_payload')}: [[{raw_path}]]",
        ]
        if summary.strip():
            parts.extend(
                [
                    "",
                    f"{translate(self.content_language, 'summary')}:",
                    summary.strip(),
                ]
            )
        return "\n".join(parts)

    def _refresh_qmd_index(self) -> None:
        result = QmdService(self.vault_path).refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
        elif result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    def _refresh_compiled_briefings(
        self,
        *,
        note_path: str,
        source_excerpt: str,
    ) -> None:
        try:
            service = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
                ai_cli=self.ai_cli,
            )
            result = service.enqueue_refresh(
                source_path=note_path,
                source_excerpt=source_excerpt,
            )
            service.spawn_background_drain()
        except Exception as exc:
            logger.warning("compiled refresh failed for %s: %s", note_path, exc)
            return
        if result["errors"] and result["errors"] not in (
            ["ai-cli-unavailable"],
            ["empty-source"],
            ["unsupported-path"],
        ):
            logger.warning(
                "compiled refresh failed for %s: %s",
                note_path,
                " | ".join(result["errors"]),
            )

    def _build_path(
        self,
        *,
        root: Path,
        timestamp: datetime,
        stem: str,
        suffix: str,
        nested_by_month: bool,
    ) -> Path:
        folder = (
            root / timestamp.strftime("%Y") / timestamp.strftime("%m")
            if nested_by_month
            else root / timestamp.date().isoformat()
        )
        base_name = timestamp.strftime("%Y-%m-%d-%H%M%S")
        path = folder / f"{base_name}-{stem}{suffix}"
        counter = 1
        while path.exists():
            path = folder / f"{base_name}-{stem}-{counter}{suffix}"
            counter += 1
        return path

    @staticmethod
    def _source_stem(url: str, digest: str) -> str:
        parsed = urlparse(url)
        raw = f"{parsed.hostname or 'web'}-{parsed.path.strip('/') or 'index'}"
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", raw).strip("-")
        return f"{(cleaned or 'web')[:80]}-{digest[:10]}"
