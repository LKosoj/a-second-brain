"""Reusable web content extraction helpers for browser-rendered pages."""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import time
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpcore
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
MAX_CONTENT_BYTES = 10 * 1024 * 1024
MAX_DIRECT_REDIRECTS = 5
DIRECT_STREAM_DEADLINE_SECONDS = 60.0
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
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
logger = logging.getLogger(__name__)


def is_public_ip(value: str) -> bool:
    """True for a globally routable address; raises ValueError for non-IPs."""
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NETWORK:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_allowed_ip(value: str) -> bool:
    try:
        return is_public_ip(value)
    except ValueError:
        return False


class _PinnedPublicBackend(httpcore.NetworkBackend):
    """Resolve once, check every answer, and connect to the checked address.

    Checking the name before the request and letting the client resolve it again
    leaves a window for DNS rebinding (public answer first, internal second).
    The TLS server name and Host header still come from the URL, so
    certificates and virtual hosts work as usual.
    """

    def __init__(
        self,
        is_allowed: Callable[[str], bool] = _is_allowed_ip,
        inner: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._is_allowed = is_allowed
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        addresses = [str(info[4][0]) for info in infos]
        if not addresses or not all(self._is_allowed(a) for a in addresses):
            raise httpcore.ConnectError(f"{host} resolves to a non-public address")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                # ConnectTimeout is not a ConnectError; a dead first address
                # (say an unreachable IPv6) must not hide the working ones.
                last_error = exc
        if isinstance(last_error, httpcore.ConnectTimeout):
            raise httpcore.ConnectTimeout(f"cannot connect to {host}") from last_error
        raise httpcore.ConnectError(f"cannot connect to {host}") from last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _PublicOnlyTransport(httpx.HTTPTransport):
    """HTTP transport that can only reach public addresses (see backend)."""

    def __init__(
        self,
        *,
        is_allowed: Callable[[str], bool] = _is_allowed_ip,
        backend: _PinnedPublicBackend | None = None,
    ) -> None:
        super().__init__()
        # `_pool` is private in httpx, but there is no public way to pass a
        # network backend; the tests with real connections catch a change.
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=backend or _PinnedPublicBackend(is_allowed),
        )


# Used by calendar_ics: the same address check for another downloader.
PublicOnlyTransport = _PublicOnlyTransport


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


def _content_length_too_large(value: str) -> bool:
    try:
        declared_size = int(value)
    except ValueError:
        return False
    return declared_size > MAX_CONTENT_BYTES


def _direct_extract(
    url: str,
    *,
    timeout: float,
    allowed_url: Callable[[str], bool] | None = None,
) -> WebContentResult:
    final_url = url
    # An explicit transport also stops httpx from reading HTTP(S)_PROXY, so with
    # a caller-supplied address check pages are fetched directly: a proxy would
    # resolve the name itself and bypass the pinned address. The PROXY_URL
    # reader (`_proxy_extract`) is a separate client and is not affected.
    transport = _PublicOnlyTransport() if allowed_url is not None else None
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers=DEFAULT_HEADERS,
            transport=transport,
        ) as client:
            request = client.build_request("GET", url)
            for _ in range(MAX_DIRECT_REDIRECTS + 1):
                response = client.send(request, stream=True)
                if response.next_request is not None:
                    next_request = response.next_request
                    response.close()
                    next_url = str(next_request.url)
                    if allowed_url is not None and not allowed_url(next_url):
                        return WebContentResult(
                            url=next_url,
                            title="",
                            content="",
                            source="blocked-url",
                        )
                    request = next_request
                    continue

                try:
                    response.raise_for_status()
                    final_url = str(response.url)
                    if allowed_url is not None and not allowed_url(final_url):
                        return WebContentResult(
                            url=final_url,
                            title="",
                            content="",
                            source="blocked-url",
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None and _content_length_too_large(
                        content_length
                    ):
                        return WebContentResult(
                            url=final_url,
                            title="",
                            content="",
                            source="too-large",
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    is_text_content = content_type.startswith("text/")
                    if (
                        content_type
                        and "html" not in content_type
                        and not is_text_content
                    ):
                        return WebContentResult(
                            url=final_url,
                            title="",
                            content="",
                            source="direct",
                        )

                    chunks: list[bytes] = []
                    total_bytes = 0
                    deadline = time.monotonic() + DIRECT_STREAM_DEADLINE_SECONDS
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > MAX_CONTENT_BYTES:
                            return WebContentResult(
                                url=final_url,
                                title="",
                                content="",
                                source="too-large",
                            )
                        if time.monotonic() > deadline:
                            return WebContentResult(
                                url=final_url,
                                title="",
                                content="",
                                source="timeout",
                            )
                        chunks.append(chunk)
                    raw_bytes = b"".join(chunks)
                    raw_text = raw_bytes.decode(
                        response.encoding or "utf-8",
                        errors="replace",
                    )
                finally:
                    response.close()
                break
            else:
                return WebContentResult(
                    url=str(request.url),
                    title="",
                    content="",
                    source="direct",
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


def tavily_search(
    query: str,
    *,
    api_key: str,
    max_results: int = 5,
    timeout: float = 20.0,
) -> list[dict[str, str]]:
    """Best-effort Tavily web search (T5 "Еженедельный уход за вики", action
    4: find sources for a page that barely has any). Modeled on
    ``_tavily_extract`` above -- a missing key or any network/parse failure
    is not the caller's problem, so this never raises and just returns an
    empty list instead.
    """
    if not api_key:
        return []
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {"query": query, "max_results": max_results}
    try:
        with httpx.Client() as client:
            resp = client.post(
                "https://api.tavily.com/search",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as exc:
        logger.warning("Tavily search failed for %r: %s", query, exc)
        return []

    results = data.get("results") or []
    if not isinstance(results, list):
        return []
    found: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        found.append(
            {
                "url": url,
                "title": str(item.get("title") or "").strip(),
                "content": str(item.get("content") or "").strip(),
            }
        )
    return found


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
