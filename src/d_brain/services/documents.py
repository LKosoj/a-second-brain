"""Storage and orchestration for uploaded document imports."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from d_brain.control_plane.contracts import WorkflowSpec
from d_brain.control_plane.registry import get_workflow
from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.cli_runner import CliRunner
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.document_extractors import (
    DOCUMENT_DOWNLOAD_MAX_BYTES,
    DOCUMENT_EXTRACTION_TIMEOUT_SECONDS,
    detect_document_format,
    summarize_plain_text,
)
from d_brain.services.frontmatter import (
    require_safe_vault_root,
    write_import_vault_markdown,
    write_vault_file_bytes,
    write_vault_file_text,
)
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import (
    normalize_language,
    prompt_language_name,
    translate,
)
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import SourceInfo, format_source_markdown

logger = logging.getLogger(__name__)
DOCUMENT_SUMMARY_TIMEOUT = 120
DOCUMENT_SUMMARY_INPUT_CHARS = 40_000
DOCUMENT_SUMMARY_FALLBACK_CHARS = 12_000


class UnsupportedDocumentError(ValueError):
    """Telegram document format is outside the supported allowlist."""


class DocumentTooLargeError(ValueError):
    """Telegram document exceeds the supported Bot API download budget."""


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    """Normalized extraction contract for one uploaded document."""

    plain_text: str
    title: str
    format: str
    warnings: list[str]
    metadata: dict[str, Any]
    truncated: bool
    source_path: str


@dataclass(frozen=True, slots=True)
class DocumentArchiveResult:
    """Persisted document import artifacts."""

    extraction: DocumentExtractionResult
    original_path: str
    text_path: str
    note_path: str
    daily_summary: str
    daily_content: str


@dataclass(frozen=True, slots=True)
class StagedDocumentUpload:
    """Document saved durably before background extraction continues."""

    file_format: str
    original_path: str


class DocumentArchiveService:
    """Persist originals, extracted text, and qmd-searchable summaries."""

    CONTROL_PLANE_WORKFLOW = "integration.documents.archive"

    def __init__(
        self,
        vault_path: Path,
        content_language: str = "ru",
        ai_cli: str = "claude",
    ) -> None:
        self.vault_path = Path(vault_path).absolute()
        self.content_language = normalize_language(content_language)
        self._manifest: VaultManifest | None = None
        self.runner = CliRunner(self.vault_path, ai_cli)
        self.documents_root = self.vault_path / "imports" / "documents"
        self.raw_root = self.documents_root / "raw"
        self.text_root = self.documents_root / "text"
        self.notes_root = self.documents_root / "notes"
        self._workflow = self._load_control_plane_workflow()

    @classmethod
    def _load_control_plane_workflow(cls) -> WorkflowSpec:
        workflow = get_workflow(cls.CONTROL_PLANE_WORKFLOW)
        expected_entrypoint = f"{cls.__module__}.{cls.__name__}.archive_document"
        if workflow.entrypoint != expected_entrypoint:
            raise ValueError(
                "Control-plane registry drift for document archive: "
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

    def validate_input(
        self,
        *,
        file_name: str,
        mime_type: str = "",
        file_size: int = 0,
    ) -> str:
        """Resolve the canonical supported format or raise one clear error."""

        file_format = detect_document_format(file_name, mime_type)
        if file_format is None:
            raise UnsupportedDocumentError(
                "Поддерживаются только pdf/docx/xlsx/txt/pptx/md/html/mpp."
            )
        if file_size and file_size > DOCUMENT_DOWNLOAD_MAX_BYTES:
            raise DocumentTooLargeError(
                "Файл превышает текущий лимит Bot API на скачивание (20 MB)."
            )
        return file_format

    def archive_document(
        self,
        *,
        data: bytes,
        file_name: str,
        mime_type: str,
        file_size: int,
        timestamp: datetime,
        source: SourceInfo | None = None,
        name_hint: str | None = None,
        caption: str | None = None,
        refresh_qmd: bool = True,
    ) -> DocumentArchiveResult:
        """Save original file, extract plain text, and persist summary artifacts."""

        staged = self.stage_document_upload(
            data=data,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            timestamp=timestamp,
            name_hint=name_hint,
        )
        return self.archive_staged_document(
            original_path=staged.original_path,
            file_name=file_name,
            file_format=staged.file_format,
            timestamp=timestamp,
            source=source,
            name_hint=name_hint,
            caption=caption,
            refresh_qmd=refresh_qmd,
        )

    def stage_document_upload(
        self,
        *,
        data: bytes,
        file_name: str,
        mime_type: str,
        file_size: int,
        timestamp: datetime,
        name_hint: str | None = None,
    ) -> StagedDocumentUpload:
        """Persist the original file before any slow extraction work starts."""
        self._manifest_for_writes()

        file_format = self.validate_input(
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size or len(data),
        )
        original_path = self._save_original(
            data,
            timestamp=timestamp,
            file_name=file_name,
            name_hint=name_hint,
        )
        return StagedDocumentUpload(
            file_format=file_format,
            original_path=original_path,
        )

    def archive_staged_document(
        self,
        *,
        original_path: str,
        file_name: str,
        file_format: str,
        timestamp: datetime,
        source: SourceInfo | None = None,
        name_hint: str | None = None,
        caption: str | None = None,
        refresh_qmd: bool = True,
    ) -> DocumentArchiveResult:
        """Finish extraction and note creation from an already persisted original."""
        self._manifest_for_writes()

        payload = self._extract_payload(
            original_path=original_path,
            file_format=file_format,
            original_name=file_name,
        )
        extraction = DocumentExtractionResult(
            plain_text=str(payload.get("plain_text", "") or ""),
            title=str(payload.get("title", "") or self._title_from_name(file_name)),
            format=str(payload.get("format", file_format) or file_format),
            warnings=[str(item) for item in payload.get("warnings", []) if str(item)],
            metadata=dict(payload.get("metadata", {}) or {}),
            truncated=bool(payload.get("truncated", False)),
            source_path=original_path,
        )
        text_path = self._save_plain_text_sidecar(
            extraction,
            timestamp=timestamp,
            file_name=file_name,
            name_hint=name_hint,
        )
        summary = self._summary(extraction)
        note_path = self._save_summary_note(
            extraction,
            timestamp=timestamp,
            file_name=file_name,
            text_path=text_path,
            summary=summary,
            source=source,
            caption=caption,
        )
        daily_summary = self._daily_summary(extraction, summary=summary)
        daily_content = self._build_daily_content(
            extraction,
            file_name=file_name,
            note_path=note_path,
            text_path=text_path,
            daily_summary=daily_summary,
            source=source,
            caption=caption,
        )
        self._refresh_compiled_briefings(
            note_path=note_path,
            source_excerpt=self._clip_compiled_excerpt(summary, extraction.plain_text),
        )
        if refresh_qmd:
            self._refresh_qmd_index()
        return DocumentArchiveResult(
            extraction=extraction,
            original_path=original_path,
            text_path=text_path,
            note_path=note_path,
            daily_summary=daily_summary,
            daily_content=daily_content,
        )

    def _refresh_qmd_index(self) -> None:
        """Keep qmd current after standalone searchable document writes."""
        result = QmdService(self.vault_path).refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    def _refresh_compiled_briefings(
        self,
        *,
        note_path: str,
        source_excerpt: str,
    ) -> None:
        """Best-effort queue enqueue after a searchable note write."""
        try:
            service = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
                ai_cli=self.runner.ai_cli,
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

    @staticmethod
    def _clip_compiled_excerpt(summary: str, plain_text: str) -> str:
        """Feed the compiler a bounded summary-first excerpt."""
        summary_text = str(summary or "").strip()
        if not plain_text:
            return summary_text
        body = str(plain_text or "").strip()
        source = summary_text if summary_text else ""
        if source:
            source += "\n\n"
        source += body[:4000]
        return source.strip()

    def build_result_message(
        self,
        result: DocumentArchiveResult,
        *,
        file_name: str,
    ) -> str:
        """Render one concise Telegram follow-up after background extraction."""

        status = (
            "⚠️ Документ сохранён, но извлечение частичное."
            if self._has_problem_warnings(result.extraction)
            else "✅ Документ сохранён."
        )
        parts = [
            status,
            f"Файл: {file_name}",
            f"Формат: {result.extraction.format}",
            f"Заголовок: {result.extraction.title}",
        ]
        if result.daily_summary:
            parts.extend(["", "Саммари:", result.daily_summary])
        if result.extraction.warnings:
            parts.append("")
            parts.append("Предупреждения:")
            parts.extend(f"- {warning}" for warning in result.extraction.warnings)
        parts.extend(["", f"Заметка: {result.note_path}"])
        if result.text_path:
            parts.append(
                f"{translate(self.content_language, 'extracted_text')}: "
                f"{result.text_path}"
            )
        return "\n".join(parts)

    def _save_original(
        self,
        data: bytes,
        *,
        timestamp: datetime,
        file_name: str,
        name_hint: str | None,
    ) -> str:
        path = self._build_path(
            root=self.raw_root,
            timestamp=timestamp,
            suffix=Path(file_name).suffix or ".bin",
            stem=file_name,
            name_hint=name_hint,
            nested_by_month=False,
        )
        write_vault_file_bytes(
            self.vault_path,
            path,
            data,
            require_absent=True,
        )
        return path.relative_to(self.vault_path).as_posix()

    def _save_plain_text_sidecar(
        self,
        extraction: DocumentExtractionResult,
        *,
        timestamp: datetime,
        file_name: str,
        name_hint: str | None,
    ) -> str:
        if not extraction.plain_text:
            return ""
        path = self._build_path(
            root=self.text_root,
            timestamp=timestamp,
            suffix=".txt",
            stem=file_name,
            name_hint=name_hint,
            nested_by_month=False,
        )
        write_vault_file_text(
            self.vault_path,
            path,
            extraction.plain_text + "\n",
            require_absent=True,
        )
        return path.relative_to(self.vault_path).as_posix()

    def _save_summary_note(
        self,
        extraction: DocumentExtractionResult,
        *,
        timestamp: datetime,
        file_name: str,
        text_path: str,
        summary: str,
        source: SourceInfo | None,
        caption: str | None,
    ) -> str:
        path = self._build_path(
            root=self.notes_root,
            timestamp=timestamp,
            suffix=".md",
            stem=file_name,
            name_hint=None,
            nested_by_month=True,
        )
        write_import_vault_markdown(
            self.vault_path,
            path,
            self._build_summary_note(
                extraction,
                text_path=text_path,
                summary=summary,
                source=source,
                caption=caption,
            ),
            manifest=self._manifest_for_writes(),
        )
        return path.relative_to(self.vault_path).as_posix()

    def _extract_payload(
        self,
        *,
        original_path: str,
        file_format: str,
        original_name: str,
    ) -> dict[str, Any]:
        raw_path = self.vault_path / original_path
        env = os.environ.copy()
        src_path = str(Path(__file__).resolve().parents[2])
        current_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = (
            f"{src_path}:{current_pythonpath}" if current_pythonpath else src_path
        )
        command = [
            sys.executable,
            "-m",
            "d_brain.run_document_extract",
            "--input",
            str(raw_path),
            "--format",
            file_format,
            "--name",
            original_name,
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=self.vault_path,
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=DOCUMENT_EXTRACTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Document extraction timed out for %s", original_path)
            return {
                "plain_text": "",
                "title": self._title_from_name(original_name),
                "format": file_format,
                "warnings": [
                    "Extraction timed out after "
                    f"{DOCUMENT_EXTRACTION_TIMEOUT_SECONDS}s."
                ],
                "metadata": {},
                "truncated": False,
            }

        if proc.returncode != 0:
            message = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or "unknown extractor error"
            )
            logger.warning(
                "Document extractor failed for %s: %s",
                original_path,
                message,
            )
            return {
                "plain_text": "",
                "title": self._title_from_name(original_name),
                "format": file_format,
                "warnings": [f"Extractor failed: {message}"],
                "metadata": {},
                "truncated": False,
            }

        try:
            return extract_first_json_dict(
                proc.stdout,
                error_context="document extractor output",
            )
        except ValueError:
            logger.warning(
                "Document extractor returned invalid JSON for %s",
                original_path,
            )
            return {
                "plain_text": "",
                "title": self._title_from_name(original_name),
                "format": file_format,
                "warnings": ["Extractor returned invalid JSON."],
                "metadata": {},
                "truncated": False,
            }

    def _build_summary_note(
        self,
        extraction: DocumentExtractionResult,
        *,
        text_path: str,
        summary: str,
        source: SourceInfo | None,
        caption: str | None,
    ) -> str:
        metadata_lines = [
            f"- {translate(self.content_language, 'format')}: `{extraction.format}`",
            (
                f"- {translate(self.content_language, 'source_path')}: "
                f"[[{extraction.source_path}]]"
            ),
        ]
        if text_path:
            metadata_lines.append(
                f"- {translate(self.content_language, 'extracted_text')}: "
                f"[[{text_path}]]"
            )
        for key, value in extraction.metadata.items():
            metadata_lines.append(f"- `{key}`: {self._metadata_value(value)}")

        parts = [
            "---",
            "type: note",
            f"format: {extraction.format}",
            f"source_path: {extraction.source_path}",
        ]
        if text_path:
            parts.append(f"text_path: {text_path}")
        parts.extend(
            [
                f"truncated: {'true' if extraction.truncated else 'false'}",
                "---",
                f"# {extraction.title}",
            ]
        )

        source_block = format_source_markdown(source, language=self.content_language)
        if source_block:
            parts.extend(["", source_block])
        if caption:
            parts.extend(["", "## Комментарий", caption.strip()])
        parts.extend(
            [
                "",
                f"## {translate(self.content_language, 'summary')}",
                summary or translate(self.content_language, "summary_unavailable"),
            ]
        )
        parts.extend(
            [
                "",
                f"## {translate(self.content_language, 'metadata')}",
                *metadata_lines,
            ]
        )
        if extraction.warnings:
            parts.extend(
                [
                    "",
                    f"## {translate(self.content_language, 'warnings')}",
                    *[f"- {warning}" for warning in extraction.warnings],
                ]
            )
        return "\n".join(parts).rstrip() + "\n"

    def _build_daily_content(
        self,
        extraction: DocumentExtractionResult,
        *,
        file_name: str,
        note_path: str,
        text_path: str,
        daily_summary: str,
        source: SourceInfo | None,
        caption: str | None,
    ) -> str:
        title = extraction.title or self._title_from_name(file_name)
        parts = [f"📄 [[{note_path}|{title}]]"]
        parts.append(
            f"> {translate(self.content_language, 'original_file')}: "
            f"[[{extraction.source_path}|{file_name}]]"
        )
        if text_path:
            parts.append(
                f"> {translate(self.content_language, 'extracted_text')}: "
                f"[[{text_path}]]"
            )
        if caption and caption.strip():
            parts.extend(["", caption.strip()])
        if daily_summary:
            parts.extend(
                [
                    "",
                    f"{translate(self.content_language, 'summary')}:",
                    daily_summary,
                ]
            )
        if extraction.warnings:
            parts.extend(
                [
                    "",
                    f"{translate(self.content_language, 'warnings')}:",
                    *[f"- {warning}" for warning in extraction.warnings],
                ]
            )
        return "\n".join(parts).strip()

    def _summary(self, extraction: DocumentExtractionResult) -> str:
        if not extraction.plain_text:
            if extraction.warnings:
                return extraction.warnings[0]
            return translate(self.content_language, "summary_unavailable")

        try:
            summary = self._llm_summary(extraction)
        except Exception as exc:
            logger.warning(
                "Document summary fallback used for %s: %s",
                extraction.source_path,
                exc,
            )
            summary = ""
        if summary:
            return summary
        fallback = summarize_plain_text(
            extraction.plain_text,
            limit=DOCUMENT_SUMMARY_FALLBACK_CHARS,
        )
        if fallback:
            return fallback
        if extraction.warnings:
            return extraction.warnings[0]
        return translate(self.content_language, "summary_unavailable")

    def _daily_summary(
        self,
        extraction: DocumentExtractionResult,
        *,
        summary: str,
    ) -> str:
        clipped = summarize_plain_text(summary, limit=0)
        if clipped:
            return clipped
        if extraction.warnings:
            return extraction.warnings[0]
        return translate(self.content_language, "summary_unavailable")

    def _llm_summary(self, extraction: DocumentExtractionResult) -> str:
        prompt = (
            "Summarize one extracted document.\n"
            f"Write the summary in {prompt_language_name(self.content_language)}.\n"
            "Return only JSON with one string field:\n"
            '{\n  "summary": "..."\n}\n\n'
            "Rules:\n"
            "- Make it detailed enough that a reader understands both the overall "
            "purpose of the document and the important specifics.\n"
            "- Start with 2-3 sentences explaining what the document is about and "
            "what it says overall.\n"
            "- Then add 4-7 lines starting with '- ' that capture the key facts, "
            "arguments, decisions, requirements, risks, dates, numbers, or next "
            "actions from the document.\n"
            "- Preserve important names, numbers, dates, commitments, and domain "
            "terms when they appear in the extracted text.\n"
            "- Ignore repeated boilerplate, navigation, page furniture, and other "
            "non-substantive extraction noise unless it changes the meaning.\n"
            "- If the extraction is partial or noisy, say that briefly instead of "
            "inventing detail.\n"
            "- Do not invent facts outside the extracted text.\n"
            "- Do not mention that you are an AI.\n"
            "- Do not use markdown fences.\n\n"
            "[TITLE]\n"
            f"{extraction.title}\n\n"
            "[FORMAT]\n"
            f"{extraction.format}\n\n"
            "[CONTENT]\n"
            f"{extraction.plain_text[:DOCUMENT_SUMMARY_INPUT_CHARS]}\n"
        )
        output = self.runner.run(prompt, timeout=DOCUMENT_SUMMARY_TIMEOUT)
        payload = extract_first_json_dict(
            output,
            error_context="document summary output",
        )
        return str(payload.get("summary", "")).strip()

    def _build_path(
        self,
        *,
        root: Path,
        timestamp: datetime,
        suffix: str,
        stem: str,
        name_hint: str | None,
        nested_by_month: bool,
    ) -> Path:
        safe_stem = self._safe_stem(stem)
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        base_name = timestamp.strftime("%Y-%m-%d-%H%M%S")
        if name_hint:
            hint = re.sub(r"[^0-9A-Za-z_-]+", "-", name_hint).strip("-")
            if hint:
                base_name = f"{base_name}-{hint}"
        filename = f"{base_name}-{safe_stem}{suffix}"
        folder = (
            root / timestamp.strftime("%Y") / timestamp.strftime("%m")
            if nested_by_month
            else root / timestamp.date().isoformat()
        )
        path = folder / filename
        counter = 1
        while path.exists():
            path = folder / f"{base_name}-{safe_stem}-{counter}{suffix}"
            counter += 1
        return path

    @staticmethod
    def _safe_stem(name: str) -> str:
        stem = Path(name or "document").stem
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", stem).strip("-")
        return cleaned or "document"

    @staticmethod
    def _title_from_name(name: str) -> str:
        stem = Path(name or "document").stem
        return re.sub(r"[_-]+", " ", stem).strip() or "Document"

    @staticmethod
    def _metadata_value(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _has_problem_warnings(extraction: DocumentExtractionResult) -> bool:
        if extraction.truncated or not extraction.plain_text:
            return True
        markers = ("timed out", "failed", "exceeded", "skipped", "no extracted text")
        return any(
            marker in warning.lower()
            for warning in extraction.warnings
            for marker in markers
        )
