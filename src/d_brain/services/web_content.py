"""Reusable web content extraction helpers for browser-rendered pages."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
DEFAULT_TIMEOUT = 20.0
JINA_READER_URL = "https://r.jina.ai"
BOILERPLATE_MARKERS = (
    "enable javascript",
    "something went wrong",
    "give it another shot",
    "privacy related extensions",
    "accept all cookies",
    "refuse non-essential cookies",
    "log in",
    "sign up",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebContentResult:
    """Normalized extracted content for one URL."""

    url: str
    title: str
    content: str
    source: str


@dataclass(frozen=True, slots=True)
class WebContentConfig:
    """Optional reader backends for hard-to-scrape pages."""

    tavily_api_key: str = ""
    jina_api_key: str = ""
    zai_api_key: str = ""
    proxy_url: str = ""


def extract_web_content(
    url: str,
    *,
    config: WebContentConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allowed_url: Callable[[str], bool] | None = None,
) -> WebContentResult:
    """Extract readable page content with API-reader fallbacks."""

    normalized_url = _normalize_url(url)
    if not normalized_url.startswith("http") or (
        allowed_url is not None and not allowed_url(normalized_url)
    ):
        return WebContentResult(
            url=normalized_url,
            title="",
            content="",
            source="invalid-url",
        )

    config = config or WebContentConfig()
    direct_result = _direct_extract(
        normalized_url,
        timeout=timeout,
        allowed_url=allowed_url,
    )
    if _is_usable_content(direct_result):
        return direct_result

    fallback_title = direct_result.title
    readers: tuple[Callable[[], WebContentResult | None], ...] = (
        lambda: _proxy_extract(normalized_url, config=config),
        lambda: _tavily_extract(normalized_url, config=config),
        lambda: _jina_extract(normalized_url, config=config),
        lambda: _zai_extract(normalized_url, config=config),
    )
    for reader in readers:
        result = reader()
        if result is None:
            continue
        if allowed_url is not None and not allowed_url(result.url):
            continue
        title = result.title or fallback_title
        candidate = WebContentResult(
            url=result.url,
            title=title,
            content=result.content,
            source=result.source,
        )
        if _is_usable_content(candidate, allow_short_without_title=True):
            return candidate

    return direct_result


def html_to_text(raw_html: str) -> dict[str, str]:
    """Convert raw HTML into compact title/content text blocks."""

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>",
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )
    title = (
        html.unescape(title_match.group(1)).strip()
        if title_match
        else ""
    )
    meta_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    ) or re.search(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )
    meta_description = (
        html.unescape(meta_match.group(1)).strip()
        if meta_match
        else ""
    )

    text = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        " ",
        raw_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    content = "\n".join(
        part
        for part in (meta_description, text)
        if part
    ).strip()
    return {
        "title": title,
        "content": content,
    }


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("[") and "](" in value:
        value = value.split("](", 1)[1]
    return value.rstrip(")").strip('"').strip("'").strip()


def _direct_extract(
    url: str,
    *,
    timeout: float,
    allowed_url: Callable[[str], bool] | None = None,
) -> WebContentResult:
    final_url = url
    try:
        with httpx.stream(
            "GET",
            url,
            timeout=timeout,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        ) as response:
            response.raise_for_status()
            final_url = str(response.url)
            if allowed_url is not None and not allowed_url(final_url):
                return WebContentResult(
                    url=final_url,
                    title="",
                    content="",
                    source="blocked-url",
                )
            content_type = response.headers.get("content-type", "").lower()
            is_text_content = content_type.startswith("text/")
            if content_type and "html" not in content_type and not is_text_content:
                return WebContentResult(
                    url=final_url,
                    title="",
                    content="",
                    source="direct",
                )

            chunks: list[bytes] = []
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            raw_text = raw_bytes.decode(
                response.encoding or "utf-8",
                errors="replace",
            )
    except httpx.HTTPError as exc:
        logger.warning("Direct content fetch failed for %s: %s", url, exc)
        return WebContentResult(url=url, title="", content="", source="direct")

    extracted = html_to_text(raw_text) if "<html" in raw_text.lower() else {
        "title": final_url,
        "content": raw_text.strip(),
    }
    return WebContentResult(
        url=final_url,
        title=extracted["title"],
        content=extracted["content"],
        source="direct",
    )


def _is_usable_content(
    result: WebContentResult,
    *,
    allow_short_without_title: bool = False,
) -> bool:
    text = result.content.strip()
    if not text:
        return False

    lowered = text.lower()
    marker_hits = sum(marker in lowered for marker in BOILERPLATE_MARKERS)
    if marker_hits >= 2:
        return False
    if marker_hits >= 1 and len(text) < 500:
        return False
    if allow_short_without_title:
        return True
    if not result.title.strip() and len(text) < 400:
        return False
    return True


def _proxy_extract(url: str, *, config: WebContentConfig) -> WebContentResult | None:
    if not config.proxy_url:
        return None
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{config.proxy_url}/zai/read",
                params={"url": url},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("reader_result") or {}
    except Exception as exc:
        logger.warning("Proxy reader failed for %s: %s", url, exc)
        return None
    content = str(data.get("content") or "").strip()
    if not content:
        return None
    return WebContentResult(
        url=url,
        title=str(data.get("title") or "").strip(),
        content=content,
        source="proxy-reader",
    )


def _tavily_extract(url: str, *, config: WebContentConfig) -> WebContentResult | None:
    if not config.tavily_api_key:
        return None
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.tavily_api_key}",
    }
    payload = {"urls": [url]}
    try:
        with httpx.Client() as client:
            resp = client.post(
                "https://api.tavily.com/extract",
                headers=headers,
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as exc:
        logger.warning("Tavily extract failed for %s: %s", url, exc)
        return None

    results = data.get("results") or data.get("data") or []
    if isinstance(results, dict):
        results = [results]
    item = results[0] if results else {}
    content = str(item.get("content") or item.get("raw_content") or "").strip()
    if not content:
        return None
    return WebContentResult(
        url=url,
        title=str(item.get("title") or "").strip(),
        content=content,
        source="tavily-extract",
    )


def _jina_extract(url: str, *, config: WebContentConfig) -> WebContentResult | None:
    if not config.jina_api_key:
        return None
    headers = {
        "Authorization": f"Bearer {config.jina_api_key}",
        "Content-Type": "application/json",
        "X-Base": "final",
        "X-Engine": "browser",
        "X-Timeout": "20000",
        "X-No-Gfm": "true",
    }
    try:
        with httpx.Client() as client:
            response = client.post(
                JINA_READER_URL,
                headers=headers,
                json={"url": url},
                timeout=30.0,
            )
            response.raise_for_status()
            title, content = _parse_jina_reader_response(response.text)
    except Exception as exc:
        logger.warning("Jina reader failed for %s: %s", url, exc)
        return None
    if not content:
        return None
    return WebContentResult(
        url=url,
        title=title,
        content=content,
        source="jina-reader",
    )


def _zai_extract(url: str, *, config: WebContentConfig) -> WebContentResult | None:
    if not config.zai_api_key:
        return None
    try:
        with httpx.Client() as client:
            resp = client.post(
                "https://api.z.ai/api/paas/v4/reader",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.zai_api_key}",
                },
                json={
                    "url": url,
                    "format": "markdown",
                    "keep_images": False,
                    "timeout": 20,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("reader_result") or {}
    except Exception as exc:
        logger.warning("Z.AI reader failed for %s: %s", url, exc)
        return None
    content = str(data.get("content") or "").strip()
    if not content:
        return None
    return WebContentResult(
        url=url,
        title=str(data.get("title") or "").strip(),
        content=content,
        source="zai-reader",
    )


def _parse_jina_reader_response(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    extracted_title = ""
    content_lines: list[str] = []
    markdown_section_started = False

    for line in lines:
        if line.startswith("Title:"):
            if not markdown_section_started:
                extracted_title = line.replace("Title:", "").strip()
        elif line.startswith("URL Source:"):
            continue
        elif line.startswith("Markdown Content:"):
            markdown_section_started = True
            first_line = line.replace("Markdown Content:", "").strip()
            if first_line:
                content_lines.append(first_line)
        elif markdown_section_started:
            content_lines.append(line)

    content = "\n".join(content_lines).strip()
    return extracted_title, content
