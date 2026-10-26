"""DNS rebinding protection for the direct page fetch (no real network)."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpcore
import httpx
import pytest

from d_brain.services.link_summary import LinkSummaryService
from d_brain.services.web_content import (
    _PinnedPublicBackend,
    _PublicOnlyTransport,
    extract_web_content,
    is_public_ip,
)

PUBLIC_IP = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo


class _Server:
    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.headers.get("Host", ""))
                body = b"<html><title>t</title><body>" + b"hello " * 200 + b"</body>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _fake_dns(monkeypatch: pytest.MonkeyPatch, answers: list[list[str]]) -> list[str]:
    """`rebind.test` gets the next answer per lookup; the last one repeats."""
    calls: list[str] = []

    def fake(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != "rebind.test":
            return _real_getaddrinfo(host, *args, **kwargs)
        calls.append(host)
        addresses = answers[min(len(calls), len(answers)) - 1]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


def test_direct_extract_blocks_dns_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    with _Server() as server:
        _fake_dns(monkeypatch, [[PUBLIC_IP], ["127.0.0.1"]])
        result = extract_web_content(
            f"http://rebind.test:{server.port}/",
            allowed_url=LinkSummaryService._is_public_http_url,
        )
    assert result.content == ""
    assert server.requests == []


def test_pinned_backend_resolves_once_and_keeps_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _Server() as server:
        calls = _fake_dns(monkeypatch, [["127.0.0.1"]])
        transport = _PublicOnlyTransport(is_allowed=lambda ip: True)
        with httpx.Client(transport=transport) as client:
            response = client.get(f"http://rebind.test:{server.port}/")
    assert response.status_code == 200
    assert len(calls) == 1
    assert server.requests == [f"rebind.test:{server.port}"]


def test_backend_rejects_mixed_public_and_private_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch, [[PUBLIC_IP, "10.0.0.5"]])
    with pytest.raises(httpcore.ConnectError):
        _PinnedPublicBackend().connect_tcp("rebind.test", 80)


def test_backend_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch, [[]])
    with pytest.raises(httpcore.ConnectError):
        _PinnedPublicBackend().connect_tcp("rebind.test", 80)


def test_https_uses_hostname_for_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Stream(httpcore.NetworkStream):
        def start_tls(
            self, ssl_context: Any, server_hostname: Any = None, timeout: Any = None
        ) -> Any:
            seen["sni"] = server_hostname
            return self

        def write(self, buffer: bytes, timeout: Any = None) -> None:
            seen["request"] = seen.get("request", b"") + buffer

        def read(self, max_bytes: int, timeout: Any = None) -> bytes:
            raise httpcore.ReadError("stop here")

        def close(self) -> None:
            pass

    class Inner(httpcore.NetworkBackend):
        def connect_tcp(self, host: str, port: int, **kwargs: Any) -> Any:
            seen["connected_to"] = host
            return Stream()

    _fake_dns(monkeypatch, [[PUBLIC_IP]])
    transport = _PublicOnlyTransport(backend=_PinnedPublicBackend(inner=Inner()))
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.HTTPError):
        client.get("https://rebind.test/")

    assert seen["connected_to"] == PUBLIC_IP
    assert seen["sni"] == "rebind.test"
    assert b"Host: rebind.test" in seen["request"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (PUBLIC_IP, True),
        ("100.64.0.1", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("169.254.169.254", False),
        ("10.1.2.3", False),
        ("2606:4700:4700::1111", True),
    ],
)
def test_is_public_ip_cases(value: str, expected: bool) -> None:
    assert is_public_ip(value) is expected


class _FlakyInner(httpcore.NetworkBackend):
    """Fake inner backend: the first address times out, the second works."""

    def __init__(self, failures: dict[str, Exception]) -> None:
        self.failures = failures
        self.tried: list[str] = []

    def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        self.tried.append(host)
        if host in self.failures:
            raise self.failures[host]
        return "stream"


def test_backend_tries_next_address_after_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch, [["93.184.216.35", PUBLIC_IP]])
    inner = _FlakyInner({"93.184.216.35": httpcore.ConnectTimeout("slow")})
    backend = _PinnedPublicBackend(inner=inner)
    assert backend.connect_tcp("rebind.test", 80) == "stream"
    assert inner.tried == ["93.184.216.35", PUBLIC_IP]


def test_backend_keeps_timeout_type_when_all_addresses_time_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch, [["93.184.216.35", PUBLIC_IP]])
    inner = _FlakyInner(
        {
            "93.184.216.35": httpcore.ConnectTimeout("slow"),
            PUBLIC_IP: httpcore.ConnectTimeout("slow too"),
        }
    )
    with pytest.raises(httpcore.ConnectTimeout):
        _PinnedPublicBackend(inner=inner).connect_tcp("rebind.test", 80)
