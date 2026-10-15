"""Regression tests for audit group G3 (media-links) fixes.

Covers:
- A) yt-dlp invocations always pass --no-playlist.
- B) documents.py passes --name as a single --name=value argv token so
  filenames starting with '-' don't break argparse.
- C) web_content._direct_extract enforces a byte-size cap while streaming.
- D) SSRF hardening: CGNAT (100.64.0.0/10) is treated as non-public, and
  redirect hops are validated individually (not just the final URL).
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from d_brain import run_document_extract
from d_brain.services import web_content
from d_brain.services.documents import DocumentArchiveService
from d_brain.services.link_summary import LinkSummaryService
from d_brain.services.youtube_transcript import YouTubeTranscriptService

# ---------------------------------------------------------------------------
# A) yt-dlp --no-playlist
# ---------------------------------------------------------------------------


def test_yt_dlp_command_always_includes_no_playlist() -> None:
    command = YouTubeTranscriptService._yt_dlp_command(["-J", "--skip-download", "url"])

    assert "--no-playlist" in command


def test_video_title_and_metadata_invoke_yt_dlp_with_no_playlist(monkeypatch) -> None:
    service = YouTubeTranscriptService("ru")
    captured_commands: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ARG001
        captured_commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    playlist_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz123"
    service._video_title(playlist_url)
    service._video_metadata(playlist_url)
    service._download_subtitles(playlist_url, write_auto_sub=False)

    assert len(captured_commands) == 3
    for command in captured_commands:
        assert "--no-playlist" in command
        assert playlist_url in command


# ---------------------------------------------------------------------------
# B) documents.py --name=value argv form
# ---------------------------------------------------------------------------


def test_document_extract_payload_passes_name_as_equals_form(
    tmp_path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    service = DocumentArchiveService(vault_path)
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):  # noqa: ANN001, ARG001
        captured["command"] = command
        payload = {
            "plain_text": "hi",
            "title": "t",
            "format": "pdf",
            "warnings": [],
            "metadata": {},
            "truncated": False,
        }
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    service._extract_payload(
        original_path="imports/documents/raw/2026/01/report.pdf",
        file_format="pdf",
        original_name="-report.pdf",
    )

    command = captured["command"]
    assert "--name=-report.pdf" in command
    # Only the combined form should be present; never a bare "--name" token
    # immediately followed by a value that argparse could mistake for a flag.
    assert "--name" not in command


def test_run_document_extract_parser_accepts_dash_prefixed_name(
    tmp_path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_extract(path, *, file_format, original_name):  # noqa: ANN001
        captured["path"] = str(path)
        captured["file_format"] = file_format
        captured["original_name"] = original_name
        return {
            "plain_text": "hi",
            "title": "t",
            "format": file_format,
            "warnings": [],
            "metadata": {},
            "truncated": False,
        }

    monkeypatch.setattr(
        run_document_extract, "extract_document_payload", fake_extract
    )
    input_path = tmp_path / "report.pdf"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_document_extract",
            "--input",
            str(input_path),
            "--format",
            "pdf",
            "--name=-report.pdf",
        ],
    )

    exit_code = run_document_extract.main()

    assert exit_code == 0
    assert captured["original_name"] == "-report.pdf"


# ---------------------------------------------------------------------------
# Fake httpx primitives shared by the C/D tests below.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for httpx.Response as used by _direct_extract."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        next_request: SimpleNamespace | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.next_request = next_request
        self._chunks = chunks or []
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Minimal stand-in for httpx.Client(follow_redirects=False, ...)."""

    def __init__(self, responses: list[_FakeResponse], **_: object) -> None:
        self._responses = list(responses)
        self.sent_urls: list[str] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def build_request(self, method: str, url: str) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(url=url)

    def send(self, request: SimpleNamespace, stream: bool = True):  # noqa: ARG002
        self.sent_urls.append(str(request.url))
        return self._responses[len(self.sent_urls) - 1]


# ---------------------------------------------------------------------------
# C) size cap while streaming
# ---------------------------------------------------------------------------


def test_direct_extract_rejects_oversized_content_length_header(monkeypatch) -> None:
    response = _FakeResponse(
        url="https://example.com/big",
        headers={"content-length": str(web_content.MAX_CONTENT_BYTES + 1)},
        chunks=[b"should never be read"],
    )
    client = _FakeClient([response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    result = web_content._direct_extract("https://example.com/big", timeout=5.0)

    assert result.content == ""
    assert result.source == "too-large"
    assert response.closed is True


def test_direct_extract_stops_streaming_past_byte_cap_without_content_length(
    monkeypatch,
) -> None:
    one_mb = b"a" * (1024 * 1024)
    chunk_count = (web_content.MAX_CONTENT_BYTES // len(one_mb)) + 2
    response = _FakeResponse(
        url="https://example.com/stream",
        headers={},
        chunks=[one_mb] * chunk_count,
    )
    client = _FakeClient([response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    result = web_content._direct_extract("https://example.com/stream", timeout=5.0)

    assert result.content == ""
    assert result.source == "too-large"
    assert response.closed is True


# ---------------------------------------------------------------------------
# D) SSRF hardening: CGNAT + per-hop redirect validation
# ---------------------------------------------------------------------------


def test_is_public_http_url_rejects_cgnat_range() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")

    assert service._is_public_http_url("http://100.64.0.1/") is False
    assert service._is_public_http_url("http://100.100.100.100/") is False
    assert service._is_public_http_url("http://100.127.255.255/") is False
    # Boundaries just outside the /10 block stay public.
    assert service._is_public_http_url("http://100.63.255.255/") is True
    assert service._is_public_http_url("http://100.128.0.1/") is True


def test_direct_extract_rejects_redirect_hop_to_private_ip_before_following(
    monkeypatch,
) -> None:
    redirect_response = _FakeResponse(
        url="https://example.com/redirect",
        next_request=SimpleNamespace(url="http://127.0.0.1/private"),
    )
    client = _FakeClient([redirect_response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    def allowed(candidate_url: str) -> bool:
        return "127.0.0.1" not in candidate_url

    result = web_content._direct_extract(
        "https://example.com/redirect",
        timeout=5.0,
        allowed_url=allowed,
    )

    assert result.source == "blocked-url"
    assert result.content == ""
    # The private hop must never actually be requested.
    assert client.sent_urls == ["https://example.com/redirect"]


# ---------------------------------------------------------------------------
# Happy path: real extraction through the fake HTTP layer (no html_to_text
# mocking), with zero and one redirect.
# ---------------------------------------------------------------------------

_SAMPLE_HTML = (
    b"<html><head><title>Hello World</title></head>"
    b"<body><p>Real page content for direct extraction.</p></body></html>"
)


def test_direct_extract_happy_path_no_redirects_extracts_real_text(
    monkeypatch,
) -> None:
    response = _FakeResponse(
        url="https://example.com/page",
        headers={"content-type": "text/html; charset=utf-8"},
        chunks=[_SAMPLE_HTML],
    )
    client = _FakeClient([response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    result = web_content._direct_extract(
        "https://example.com/page",
        timeout=5.0,
        allowed_url=lambda _: True,
    )

    assert result.source == "direct"
    assert result.url == "https://example.com/page"
    assert result.title == "Hello World"
    assert "Real page content for direct extraction." in result.content
    assert client.sent_urls == ["https://example.com/page"]


def test_direct_extract_happy_path_one_allowed_redirect_extracts_real_text(
    monkeypatch,
) -> None:
    redirect_response = _FakeResponse(
        url="https://example.com/start",
        next_request=SimpleNamespace(url="https://example.com/final"),
    )
    final_response = _FakeResponse(
        url="https://example.com/final",
        headers={"content-type": "text/html; charset=utf-8"},
        chunks=[_SAMPLE_HTML],
    )
    client = _FakeClient([redirect_response, final_response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    checked_urls: list[str] = []

    def allowed(candidate_url: str) -> bool:
        checked_urls.append(candidate_url)
        return True

    result = web_content._direct_extract(
        "https://example.com/start",
        timeout=5.0,
        allowed_url=allowed,
    )

    assert result.source == "direct"
    assert result.url == "https://example.com/final"
    assert result.title == "Hello World"
    assert "Real page content for direct extraction." in result.content
    assert client.sent_urls == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    # The redirect target must be validated before it is followed.
    assert "https://example.com/final" in checked_urls


# ---------------------------------------------------------------------------
# Too many redirects: the hop beyond the cap is never requested.
# ---------------------------------------------------------------------------


def test_direct_extract_stops_after_max_redirects_and_never_requests_extra_hop(
    monkeypatch,
) -> None:
    hops = [f"https://example.com/hop{i}" for i in range(6)]
    unreached_hop = "http://127.0.0.1/never-fetched"
    chain = [*hops[1:], unreached_hop]
    responses = [
        _FakeResponse(url=hops[i], next_request=SimpleNamespace(url=chain[i]))
        for i in range(len(hops))
    ]
    client = _FakeClient(responses)
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    result = web_content._direct_extract(
        hops[0],
        timeout=5.0,
        allowed_url=lambda _: True,
    )

    assert result.content == ""
    assert result.source == "direct"
    # We stop right after exhausting the redirect budget; the last known
    # (unfetched) hop is reported instead of the original request URL.
    assert result.url == unreached_hop
    assert client.sent_urls == hops
    assert unreached_hop not in client.sent_urls


def test_link_summary_fetch_page_text_rejects_redirect_through_real_pipeline(
    monkeypatch,
) -> None:
    """Exercise the real extract_web_content -> _direct_extract path.

    Unlike the existing mock-at-extract_web_content test in
    test_documents_and_links.py, this does not stub out extract_web_content
    itself, so it actually proves the per-hop redirect check inside
    _direct_extract rejects the private target instead of only the final URL.
    """
    redirect_response = _FakeResponse(
        url="https://example.com/redirect",
        next_request=SimpleNamespace(url="http://127.0.0.1/private"),
    )
    client = _FakeClient([redirect_response])
    monkeypatch.setattr(web_content.httpx, "Client", lambda **_: client)

    service = LinkSummaryService("/tmp/vault", "qwen", "ru")

    assert service._fetch_page_text("https://example.com/redirect") is None
    assert client.sent_urls == ["https://example.com/redirect"]
