"""YouTube transcript fetching and archival helpers."""

from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

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

YOUTUBE_TIMEOUT = 120
logger = logging.getLogger(__name__)
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:m\.)?"
    r"(?:youtube\.com/watch\?[^\s]*v=[\w-]{11}[^\s]*"
    r"|youtube\.com/shorts/[\w-]{11}[^\s]*"
    r"|youtu\.be/[\w-]{11}[^\s]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class YouTubeTranscript:
    """Resolved transcript for one YouTube URL."""

    url: str
    title: str
    transcript: str
    source: str
    video_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class YouTubeArchiveResult:
    """Persisted YouTube transcript artifacts."""

    transcript: YouTubeTranscript
    raw_path: str
    transcript_path: str
    note_path: str
    daily_content: str


class YouTubeTranscriptService:
    """Fetch YouTube transcripts with yt-dlp and caption-track fallbacks."""

    def __init__(self, content_language: str = "ru") -> None:
        self.content_language = normalize_language(content_language)

    def extract_urls(self, text: str) -> list[str]:
        """Extract unique YouTube URLs in order of appearance.

        Strips trailing punctuation/brackets/quotes the same way as
        LinkSummaryService.extract_urls so transcript lookups match by URL.
        """
        seen: set[str] = set()
        urls: list[str] = []
        for match in YOUTUBE_URL_RE.findall(text or ""):
            cleaned = match.rstrip(".,;:!?)]}>\"'")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            urls.append(cleaned)
        return urls

    def enrich_text(self, text: str) -> tuple[str, list[YouTubeTranscript]]:
        """Append YouTube transcript blocks to text when URLs are present."""
        urls = self.extract_urls(text)
        if not urls:
            return text, []

        transcripts: list[YouTubeTranscript] = []
        blocks: list[str] = []
        for index, url in enumerate(urls, start=1):
            transcript = self.fetch_transcript(url)
            transcript_label = translate(self.content_language, "youtube_transcript")
            unknown_title = translate(self.content_language, "unknown_title")
            if transcript is None:
                blocks.append(
                    "\n".join(
                        [
                            f"{transcript_label} {index}:",
                            f"{translate(self.content_language, 'url')}: {url}",
                            translate(
                                self.content_language,
                                "transcript_unavailable",
                            ),
                        ]
                    )
                )
                continue
            transcripts.append(transcript)
            blocks.append(
                "\n".join(
                    [
                        f"{transcript_label} {index}:",
                        f"{translate(self.content_language, 'title')}: "
                        f"{transcript.title or unknown_title}",
                        (
                            f"{translate(self.content_language, 'url')}: "
                            f"{transcript.url}"
                        ),
                        f"{translate(self.content_language, 'transcript_source')}: "
                        f"{transcript.source}",
                        f"{translate(self.content_language, 'transcript')}:",
                        "```text",
                        transcript.transcript,
                        "```",
                    ]
                )
            )

        return text.rstrip() + "\n\n" + "\n\n".join(blocks), transcripts

    def fetch_transcript(self, url: str) -> YouTubeTranscript | None:
        """Resolve one YouTube transcript or return None."""
        metadata = self._video_metadata(url)
        title = self._metadata_title(metadata) or self._video_title(url)
        video_id = self._metadata_video_id(metadata)
        track = self._transcript_from_metadata(metadata)
        if track is not None:
            transcript_text, source = track
            return YouTubeTranscript(
                url=url,
                title=title,
                transcript=transcript_text,
                source=source,
                video_id=video_id,
                metadata=metadata,
            )

        manual = self._download_subtitles(url, write_auto_sub=False)
        if manual:
            return YouTubeTranscript(
                url=url,
                title=title,
                transcript=manual,
                source=translate(self.content_language, "manual_subtitles"),
                video_id=video_id,
                metadata=metadata,
            )

        auto = self._download_subtitles(url, write_auto_sub=True)
        if auto:
            return YouTubeTranscript(
                url=url,
                title=title,
                transcript=auto,
                source=translate(self.content_language, "auto_subtitles"),
                video_id=video_id,
                metadata=metadata,
            )

        return None

    def _video_title(self, url: str) -> str:
        try:
            result = self._run_yt_dlp(["--print", "%(title)s", url])
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip()

    def _video_metadata(self, url: str) -> dict[str, Any]:
        try:
            result = self._run_yt_dlp(["-J", "--skip-download", url])
        except (FileNotFoundError, subprocess.SubprocessError):
            return {}
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _metadata_title(metadata: dict[str, Any]) -> str:
        return str(metadata.get("title") or "").strip()

    @staticmethod
    def _metadata_video_id(metadata: dict[str, Any]) -> str:
        return str(metadata.get("id") or "").strip()

    def _transcript_from_metadata(
        self,
        metadata: dict[str, Any],
    ) -> tuple[str, str] | None:
        if not metadata:
            return None

        for bucket_name, source_label in (
            ("subtitles", translate(self.content_language, "manual_subtitles")),
            ("automatic_captions", translate(self.content_language, "auto_subtitles")),
        ):
            bucket = metadata.get(bucket_name)
            if not isinstance(bucket, dict):
                continue
            for language in self._preferred_caption_languages():
                for key in self._matching_caption_keys(bucket, language):
                    entries = bucket.get(key)
                    if not isinstance(entries, list):
                        continue
                    track_url = self._preferred_caption_url(entries)
                    if not track_url:
                        continue
                    transcript = self._download_caption_track(track_url)
                    if transcript:
                        return transcript, source_label
        return None

    def _preferred_caption_languages(self) -> tuple[str, ...]:
        if self.content_language == "ru":
            return ("ru", "en", "en-orig")
        return ("en", "en-orig", "ru")

    @staticmethod
    def _matching_caption_keys(
        bucket: dict[str, Any],
        language: str,
    ) -> list[str]:
        keys: list[str] = []
        exact = language if language in bucket else ""
        if exact:
            keys.append(exact)
        for key in bucket:
            if key in keys:
                continue
            if key.startswith(f"{language}-"):
                keys.append(key)
        return keys

    @staticmethod
    def _preferred_caption_url(entries: list[dict[str, Any]]) -> str:
        preferred_exts = ("json3", "vtt")
        for ext in preferred_exts:
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if str(item.get("ext") or "").strip() == ext and item.get("url"):
                    return str(item["url"])
        for item in entries:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
        return ""

    def _download_caption_track(self, url: str) -> str:
        try:
            response = httpx.get(
                url,
                timeout=YOUTUBE_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/135.0.0.0 Safari/537.36"
                    )
                },
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return ""

        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type or "fmt=json3" in url.lower():
            return self._json3_to_text(response.text)
        return self._vtt_to_text(response.text)

    def _download_subtitles(self, url: str, *, write_auto_sub: bool) -> str:
        with tempfile.TemporaryDirectory(prefix="yt-transcript-") as temp_dir:
            output_template = str(Path(temp_dir) / "%(id)s")
            command = [
                "--skip-download",
                "--sub-format",
                "vtt",
                "--sub-langs",
                "ru.*,en.*,ru,en",
                "--output",
                output_template,
            ]
            if write_auto_sub:
                command.append("--write-auto-sub")
            else:
                command.append("--write-sub")
            command.append(url)

            try:
                self._run_yt_dlp(command)
            except (FileNotFoundError, subprocess.SubprocessError):
                return ""

            vtt_files = self._subtitle_files(Path(temp_dir))
            for path in vtt_files:
                text = self._vtt_to_text(path.read_text(encoding="utf-8"))
                if text:
                    return text
        return ""

    @staticmethod
    def _subtitle_files(temp_dir: Path) -> list[Path]:
        return sorted(
            path
            for path in temp_dir.glob("*.vtt")
            if "live_chat" not in path.name
        )

    @staticmethod
    def _vtt_to_text(content: str) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if (
                not line
                or line == "WEBVTT"
                or line.startswith("Kind:")
                or line.startswith("Language:")
                or "-->" in line
            ):
                continue
            clean = re.sub(r"<[^>]*>", "", line)
            clean = html.unescape(clean).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            lines.append(clean)
        return "\n".join(lines).strip()

    @staticmethod
    def _json3_to_text(content: str) -> str:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return ""
        events = payload.get("events", [])
        if not isinstance(events, list):
            return ""

        lines: list[str] = []
        seen: set[str] = set()
        for item in events:
            if not isinstance(item, dict):
                continue
            segs = item.get("segs")
            if not isinstance(segs, list):
                continue
            text = "".join(
                str(seg.get("utf8") or "")
                for seg in segs
                if isinstance(seg, dict)
            )
            clean = html.unescape(text.replace("\n", " ")).strip()
            clean = re.sub(r"\s+", " ", clean)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            lines.append(clean)
        return "\n".join(lines).strip()

    @staticmethod
    def _yt_dlp_command(args: Iterable[str]) -> list[str]:
        return [sys.executable, "-m", "yt_dlp", *args]

    def _run_yt_dlp(self, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._yt_dlp_command(args),
            capture_output=True,
            text=True,
            timeout=YOUTUBE_TIMEOUT,
            check=True,
        )


class YouTubeArchiveService:
    """Persist YouTube transcript artifacts under imports/youtube."""

    CONTROL_PLANE_WORKFLOW = "integration.youtube.archive"

    def __init__(self, vault_path: Path, content_language: str = "ru") -> None:
        self.vault_path = Path(vault_path).absolute()
        self.content_language = normalize_language(content_language)
        self._manifest: VaultManifest | None = None
        self.youtube_root = self.vault_path / "imports" / "youtube"
        self.raw_root = self.youtube_root / "raw"
        self.transcript_root = self.youtube_root / "transcripts"
        self.notes_root = self.youtube_root / "notes"
        self._workflow: WorkflowSpec = self._load_control_plane_workflow()

    @classmethod
    def _load_control_plane_workflow(cls) -> WorkflowSpec:
        workflow = get_workflow(cls.CONTROL_PLANE_WORKFLOW)
        expected_entrypoint = f"{cls.__module__}.{cls.__name__}.archive_transcript"
        if workflow.entrypoint != expected_entrypoint:
            raise ValueError(
                "Control-plane registry drift for YouTube archive: "
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

    def archive_transcript(
        self,
        transcript: YouTubeTranscript,
        *,
        timestamp: datetime,
        summary: str,
        source: SourceInfo | None = None,
        refresh_qmd: bool = True,
    ) -> YouTubeArchiveResult:
        manifest = self._manifest_for_writes()
        raw_path = self._build_path(
            root=self.raw_root,
            timestamp=timestamp,
            suffix=".json",
            stem=transcript.video_id or transcript.title or "youtube",
            nested_by_month=False,
        )
        transcript_path = self._build_path(
            root=self.transcript_root,
            timestamp=timestamp,
            suffix=".txt",
            stem=transcript.video_id or transcript.title or "youtube",
            nested_by_month=False,
        )
        note_path = self._build_path(
            root=self.notes_root,
            timestamp=timestamp,
            suffix=".md",
            stem=transcript.video_id or transcript.title or "youtube",
            nested_by_month=True,
        )

        raw_payload = dict(transcript.metadata or {})
        raw_payload.setdefault("url", transcript.url)
        raw_payload.setdefault("title", transcript.title)
        raw_payload.setdefault("video_id", transcript.video_id)
        raw_payload.setdefault("transcript_source", transcript.source)
        raw_text = json.dumps(
            raw_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

        write_vault_file_text(
            self.vault_path,
            raw_path,
            raw_text + "\n",
            require_absent=True,
        )
        write_vault_file_text(
            self.vault_path,
            transcript_path,
            transcript.transcript.rstrip() + "\n",
            require_absent=True,
        )

        raw_rel_path = raw_path.relative_to(self.vault_path).as_posix()
        transcript_rel_path = transcript_path.relative_to(self.vault_path).as_posix()
        note_rel_path = note_path.relative_to(self.vault_path).as_posix()

        write_import_vault_markdown(
            self.vault_path,
            note_path,
            self._build_note(
                transcript=transcript,
                summary=summary,
                raw_path=raw_rel_path,
                transcript_path=transcript_rel_path,
                imported_at=timestamp,
                source=source,
            ),
            manifest=manifest,
        )
        self._refresh_compiled_briefings(
            note_path=note_rel_path,
            source_excerpt=self._compiled_excerpt(
                transcript=transcript,
                summary=summary,
            ),
        )
        if refresh_qmd:
            self._refresh_qmd_index()
        return YouTubeArchiveResult(
            transcript=transcript,
            raw_path=raw_rel_path,
            transcript_path=transcript_rel_path,
            note_path=note_rel_path,
            daily_content=self._build_daily_content(
                transcript=transcript,
                summary=summary,
                note_path=note_rel_path,
                raw_path=raw_rel_path,
                transcript_path=transcript_rel_path,
            ),
        )

    def _refresh_qmd_index(self) -> None:
        """Keep qmd current after standalone searchable YouTube writes."""
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
        """Best-effort queue enqueue after one searchable write."""
        try:
            service = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
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
    def _compiled_excerpt(*, transcript: YouTubeTranscript, summary: str) -> str:
        """Keep compiler context bounded but useful."""
        parts = [str(summary or "").strip()]
        transcript_excerpt = str(transcript.transcript or "").strip()[:3500]
        if transcript_excerpt:
            parts.append(transcript_excerpt)
        return "\n\n".join(part for part in parts if part).strip()

    def _build_note(
        self,
        *,
        transcript: YouTubeTranscript,
        summary: str,
        raw_path: str,
        transcript_path: str,
        imported_at: datetime,
        source: SourceInfo | None,
    ) -> str:
        source_block = format_source_markdown(source, language=self.content_language)
        title = transcript.title or translate(self.content_language, "unknown_title")
        parts = [
            "---",
            "type: youtube-transcript",
            "source: youtube",
            f"url: {json.dumps(transcript.url, ensure_ascii=False)}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"video_id: {json.dumps(transcript.video_id, ensure_ascii=False)}",
            f"transcript_source: {json.dumps(transcript.source, ensure_ascii=False)}",
            f"imported_at: {imported_at.isoformat()}",
            f"updated: {imported_at.date().isoformat()}",
            f"raw_path: {raw_path}",
            f"transcript_path: {transcript_path}",
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
                summary or translate(self.content_language, "summary_unavailable"),
                "",
                f"## {translate(self.content_language, 'source')}",
                f"- YouTube: {transcript.url}",
                f"- {translate(self.content_language, 'transcript_source')}: "
                f"`{transcript.source}`",
                f"- Video ID: `{transcript.video_id or 'unknown'}`",
                f"- {translate(self.content_language, 'raw_payload')}: "
                f"`[[{raw_path}]]`",
                f"- {translate(self.content_language, 'transcript')}: "
                f"`[[{transcript_path}]]`",
            ]
        )
        return "\n".join(parts).rstrip() + "\n"

    def _build_daily_content(
        self,
        *,
        transcript: YouTubeTranscript,
        summary: str,
        note_path: str,
        raw_path: str,
        transcript_path: str,
    ) -> str:
        title = transcript.title or translate(self.content_language, "unknown_title")
        parts = [f"▶️ [[{note_path}|{title}]]"]
        parts.append(f"> {translate(self.content_language, 'url')}: {transcript.url}")
        parts.append(
            f"> {translate(self.content_language, 'transcript')}: "
            f"[[{transcript_path}]]"
        )
        parts.append(
            f"> {translate(self.content_language, 'raw_payload')}: "
            f"[[{raw_path}]]"
        )
        if summary:
            parts.extend(
                [
                    "",
                    f"{translate(self.content_language, 'summary')}:",
                    summary.strip(),
                ]
            )
        return "\n".join(parts).strip()

    def _build_path(
        self,
        *,
        root: Path,
        timestamp: datetime,
        suffix: str,
        stem: str,
        nested_by_month: bool,
    ) -> Path:
        safe_stem = self._safe_stem(stem)
        base_name = timestamp.strftime("%Y-%m-%d-%H%M%S")
        folder = (
            root / timestamp.strftime("%Y") / timestamp.strftime("%m")
            if nested_by_month
            else root / timestamp.date().isoformat()
        )
        path = folder / f"{base_name}-{safe_stem}{suffix}"
        counter = 1
        while path.exists():
            path = folder / f"{base_name}-{safe_stem}-{counter}{suffix}"
            counter += 1
        return path

    @staticmethod
    def _safe_stem(name: str) -> str:
        stem = Path(name or "youtube").stem
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", stem).strip("-")
        return cleaned or "youtube"
