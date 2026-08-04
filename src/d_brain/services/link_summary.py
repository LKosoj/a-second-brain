"""Best-effort web link summaries for short Telegram messages."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from d_brain.services.cli_runner import CliRunner
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import (
    normalize_language,
    prompt_language_name,
    translate,
)
from d_brain.services.source_links import SourceInfo
from d_brain.services.web_archive import WebArchiveService
from d_brain.services.web_content import (
    WebContentConfig,
    WebContentResult,
    extract_web_content,
    html_to_text,
)
from d_brain.services.youtube_transcript import (
    YouTubeArchiveService,
    YouTubeTranscript,
    YouTubeTranscriptService,
)

LINK_SUMMARY_TIMEOUT = 45
YOUTUBE_SUMMARY_TIMEOUT = 120
GENERIC_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LinkSummary:
    """Generated summary for one URL."""

    url: str
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class LinkEnrichmentResult:
    """Structured enrichment payload for one incoming text block."""

    content: str
    transcripts: list[YouTubeTranscript]
    summaries: list[LinkSummary]
    youtube_summaries: list[LinkSummary]


def _format_summary_message(
    summaries: list[LinkSummary],
    *,
    language: str,
    youtube_urls: set[str] | None = None,
) -> str:
    """Render link summaries for a Telegram reply."""

    if not summaries:
        return ""

    normalized = normalize_language(language)
    unknown_title = translate(normalized, "unknown_title")
    youtube_urls = youtube_urls or set()
    blocks = []
    for index, summary in enumerate(summaries, start=1):
        label_key = (
            "youtube_summary" if summary.url in youtube_urls else "link_summary"
        )
        blocks.append(
            "\n".join(
                [
                    f"{translate(normalized, label_key)} {index}:",
                    f"{translate(normalized, 'title')}: "
                    f"{summary.title or unknown_title}",
                    f"{translate(normalized, 'url')}: {summary.url}",
                    f"{translate(normalized, 'summary')}:",
                    summary.summary,
                ]
            )
        )
    return "\n\n".join(blocks)


def format_link_summary_message(
    summaries: list[LinkSummary],
    *,
    language: str,
    youtube_urls: set[str] | None = None,
) -> str:
    """Render generic and YouTube link summaries for a Telegram reply."""

    return _format_summary_message(
        summaries,
        language=language,
        youtube_urls=youtube_urls,
    )


def format_youtube_summary_message(
    summaries: list[LinkSummary],
    *,
    language: str,
) -> str:
    """Render YouTube summaries for a Telegram reply."""

    return _format_summary_message(
        summaries,
        language=language,
        youtube_urls={summary.url for summary in summaries},
    )


class LinkSummaryService:
    """Append link summaries when the surrounding user text is very short."""

    def __init__(
        self,
        vault_path: Path | str,
        ai_cli: str = "claude",
        content_language: str = "ru",
        youtube_service: YouTubeTranscriptService | None = None,
        tavily_api_key: str = "",
        jina_api_key: str = "",
        zai_api_key: str = "",
        proxy_url: str = "",
    ) -> None:
        normalized_vault_path = Path(vault_path)
        self.runner = CliRunner(normalized_vault_path, ai_cli)
        self.content_language = normalize_language(content_language)
        self.web_content_config = WebContentConfig(
            tavily_api_key=tavily_api_key,
            jina_api_key=jina_api_key,
            zai_api_key=zai_api_key,
            proxy_url=proxy_url,
        )
        self.youtube = youtube_service or YouTubeTranscriptService(
            content_language=self.content_language
        )
        self.youtube_archive = YouTubeArchiveService(
            normalized_vault_path,
            content_language=self.content_language,
        )
        self.web_archive = WebArchiveService(
            normalized_vault_path,
            content_language=self.content_language,
            ai_cli=ai_cli,
        )

    def extract_urls(self, text: str) -> list[str]:
        """Extract unique generic URLs in order of appearance."""

        seen: set[str] = set()
        urls: list[str] = []
        for match in GENERIC_URL_RE.findall(text or ""):
            cleaned = match.rstrip(".,;:!?)]}>\"'")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            urls.append(cleaned)
        return urls

    def should_summarize(self, text: str) -> bool:
        """Apply the short-context rule from the product requirement."""

        if not text:
            return False
        stripped = GENERIC_URL_RE.sub("", text)
        return len(stripped.strip()) < 100

    def enrich_text(
        self,
        text: str,
        *,
        timestamp: datetime | None = None,
        source: SourceInfo | None = None,
        refresh_qmd: bool = True,
    ) -> LinkEnrichmentResult:
        """Append best-effort link summaries and archive YouTube transcripts."""

        youtube_urls = self.youtube.extract_urls(text)
        transcripts: list[YouTubeTranscript] = []
        transcript_map: dict[str, YouTubeTranscript] = {}
        for url in youtube_urls:
            try:
                transcript = self.youtube.fetch_transcript(url)
            except Exception as exc:
                logger.warning("YouTube enrichment failed for %s: %s", url, exc)
                transcript = None
            if transcript is None:
                continue
            transcripts.append(transcript)
            transcript_map[url] = transcript

        urls = self.extract_urls(text)
        should_summarize = self.should_summarize(text)
        if not urls:
            return LinkEnrichmentResult(
                content=text,
                transcripts=transcripts,
                summaries=[],
                youtube_summaries=[],
            )

        youtube_url_set = set(youtube_urls)
        daily_blocks: list[str] = []
        summary_blocks: list[str] = []
        summaries: list[LinkSummary] = []
        unknown_title = translate(self.content_language, "unknown_title")
        for index, url in enumerate(urls, start=1):
            transcript = transcript_map.get(url)
            generic_source_archived = False
            youtube_label = translate(self.content_language, "youtube_transcript")
            if transcript is None and not should_summarize and timestamp is None:
                continue
            try:
                if transcript is None and timestamp is not None:
                    summary, page = self._fetch_web_link(
                        url,
                        summarize=should_summarize,
                    )
                else:
                    summary = self._summarize_url(url, transcript)
                    page = None
            except Exception as exc:
                logger.warning("Link summary failed for %s: %s", url, exc)
                summary = None
                page = None
            if transcript is not None and timestamp is not None:
                archive_result = self.youtube_archive.archive_transcript(
                    transcript,
                    timestamp=timestamp,
                    summary=summary.summary if summary is not None else "",
                    source=source,
                    refresh_qmd=refresh_qmd,
                )
                daily_blocks.append(archive_result.daily_content)
            elif transcript is not None and timestamp is None:
                title_text = (
                    summary.title
                    if summary is not None and summary.title
                    else transcript.title or unknown_title
                )
                daily_blocks.append(
                    "\n".join(
                        [
                            f"{youtube_label} {index}:",
                            f"{translate(self.content_language, 'title')}: "
                            f"{title_text}",
                            f"{translate(self.content_language, 'url')}: {url}",
                            f"{translate(self.content_language, 'transcript_source')}: "
                            f"{transcript.source}",
                        ]
                    )
                )
            elif page is not None and timestamp is not None:
                try:
                    web_archive_result = self.web_archive.archive_page(
                        page,
                        original_url=url,
                        timestamp=timestamp,
                        summary=summary.summary if summary is not None else "",
                        source=source,
                        refresh_qmd=refresh_qmd,
                    )
                except Exception as exc:
                    logger.warning("Web archive failed for %s: %s", url, exc)
                else:
                    daily_blocks.append(web_archive_result.daily_content)
                    generic_source_archived = True

            if summary is None or not should_summarize:
                continue
            summaries.append(summary)
            label = (
                translate(self.content_language, "youtube_summary")
                if transcript is not None
                else translate(self.content_language, "link_summary")
            )
            if transcript is not None or generic_source_archived:
                continue
            summary_blocks.append(
                "\n".join(
                    [
                        f"{label} {index}:",
                        f"{translate(self.content_language, 'title')}: "
                        f"{summary.title or unknown_title}",
                        f"{translate(self.content_language, 'url')}: {summary.url}",
                        f"{translate(self.content_language, 'summary')}:",
                        summary.summary,
                    ]
                )
            )

        content = text.rstrip()
        blocks = [*daily_blocks, *summary_blocks]
        if blocks:
            content += "\n\n" + "\n\n".join(blocks)
        return LinkEnrichmentResult(
            content=content,
            transcripts=transcripts,
            summaries=summaries,
            youtube_summaries=[
                summary for summary in summaries if summary.url in youtube_url_set
            ],
        )

    def _summarize_url(
        self,
        url: str,
        transcript: YouTubeTranscript | None = None,
    ) -> LinkSummary | None:
        if transcript is not None:
            title = transcript.title or url
            summary = self._llm_youtube_summary(
                title=title,
                url=url,
                content=transcript.transcript,
            )
            if summary:
                return LinkSummary(url=url, title=title, summary=summary)
            return None

        link_summary, _ = self._fetch_web_link(url, summarize=True)
        return link_summary

    def _fetch_web_link(
        self,
        url: str,
        *,
        summarize: bool,
    ) -> tuple[LinkSummary | None, WebContentResult | None]:
        page = self._fetch_page_text(url)
        if page is None:
            return None, None
        result = WebContentResult(
            url=page.get("url", url),
            title=page.get("title", ""),
            content=page.get("content", ""),
            source=page.get("source", "unknown"),
        )
        if not summarize:
            return None, result
        try:
            summary_text = self._llm_summary(
                title=result.title,
                url=url,
                content=result.content,
            )
        except Exception as exc:
            logger.warning("Link summary failed for %s: %s", url, exc)
            return None, result
        if not summary_text:
            return None, result
        return (
            LinkSummary(
                url=url,
                title=result.title,
                summary=summary_text,
            ),
            result,
        )

    def _fetch_page_text(self, url: str) -> dict[str, str] | None:
        if not self._is_public_http_url(url):
            return None
        extracted = extract_web_content(
            url,
            config=self.web_content_config,
            timeout=LINK_SUMMARY_TIMEOUT,
            allowed_url=self._is_public_http_url,
        )
        if not self._is_public_http_url(extracted.url):
            return None
        if not extracted.content:
            return None
        return {
            "url": extracted.url,
            "title": extracted.title,
            "content": extracted.content,
            "source": extracted.source,
        }

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        """Reject non-http URLs and private/internal destinations."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname.lower() == "localhost":
            return False

        def _is_public_ip(value: str) -> bool:
            ip = ipaddress.ip_address(value)
            return not (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )

        try:
            return _is_public_ip(hostname)
        except ValueError:
            try:
                infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            except socket.gaierror:
                return False
            resolved_any = False
            for info in infos:
                address = cast(str, info[4][0])
                try:
                    if not _is_public_ip(address):
                        return False
                    resolved_any = True
                except ValueError:
                    continue
            return resolved_any

    @staticmethod
    def _html_to_text(raw_html: str) -> dict[str, str]:
        extracted = html_to_text(raw_html)
        return {
            "title": extracted["title"],
            "content": extracted["content"],
        }

    def _llm_summary(self, *, title: str, url: str, content: str) -> str:
        prompt = (
            "Summarize one web resource.\n"
            f"Write the summary in {prompt_language_name(self.content_language)}.\n"
            "Return only JSON with one string field:\n"
            '{\n  "summary": "..."\n}\n\n'
            "Rules:\n"
            "- Keep it concise and factual.\n"
            "- Preserve important names, decisions, and numbers.\n"
            "- Ignore navigation, cookie banners, login prompts, and repeated "
            "boilerplate.\n"
            "- Focus on the central claim, decision, or takeaway of the resource.\n"
            "- If the content is thin or mostly boilerplate, say that briefly "
            "instead of inventing detail.\n"
            "- Do not mention that you are an AI.\n"
            "- Do not use markdown fences.\n\n"
            "[TITLE]\n"
            f"{title}\n\n"
            "[URL]\n"
            f"{url}\n\n"
            "[CONTENT]\n"
            f"{content}\n"
        )
        output = self.runner.run(prompt, timeout=LINK_SUMMARY_TIMEOUT)
        payload = extract_first_json_dict(
            output,
            error_context="link summary output",
        )
        summary = str(payload.get("summary", "")).strip()
        return summary

    def _llm_youtube_summary(self, *, title: str, url: str, content: str) -> str:
        prompt = (
            "Summarize one YouTube video transcript.\n"
            f"Write the summary in {prompt_language_name(self.content_language)}.\n"
            "Return only JSON with one string field:\n"
            '{\n  "summary": "..."\n}\n\n'
            "Rules:\n"
            "- Make it detailed enough that a reader understands both the main "
            "message and the supporting substance.\n"
            "- Start with 2-3 sentences covering the topic, speaker focus, and "
            "overall conclusion.\n"
            "- Then add 5-8 lines starting with '- ' that capture the concrete "
            "ideas, comparisons, examples, tools, constraints, and practical "
            "takeaways from the talk.\n"
            "- Preserve important names, decisions, steps, and numbers when they "
            "appear in the transcript.\n"
            "- Mention what problem is being solved, how the speaker explains the "
            "approach, and what tradeoffs or limitations are highlighted.\n"
            "- If the transcript is partial or noisy, say that briefly instead of "
            "inventing detail.\n"
            "- Do not quote long fragments verbatim.\n"
            "- Do not mention that you are an AI.\n"
            "- Do not use markdown fences.\n\n"
            "[TITLE]\n"
            f"{title}\n\n"
            "[URL]\n"
            f"{url}\n\n"
            "[TRANSCRIPT]\n"
            f"{content}\n"
        )
        output = self.runner.run(prompt, timeout=YOUTUBE_SUMMARY_TIMEOUT)
        payload = extract_first_json_dict(
            output,
            error_context="YouTube summary output",
        )
        return str(payload.get("summary", "")).strip()
