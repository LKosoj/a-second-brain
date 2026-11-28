"""Tests for the private LacEditor network entry point."""

from __future__ import annotations

import hashlib
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from d_brain.config import Settings
from d_brain.network_server import NetworkServer
from d_brain.services.network_requests import NetworkJobStore, NetworkRequestService
from d_brain.services.vault_find import FindHit, FindSearchResult
from d_brain.services.vault_lock import VaultLockTimeout


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="telegram",
        owner_telegram_id=1,
        network_api_token="network-token",
        network_api_host="127.0.0.1",
        network_api_port=8765,
        vault_path=tmp_path / "vault",
    )


def test_job_store_is_idempotent(tmp_path: Path) -> None:
    store = NetworkJobStore(tmp_path / "vault")

    first = store.create("request-1")
    second = store.create("request-1")

    assert first["job_id"] == second["job_id"]
    assert store.get(str(first["job_id"])) == first
    assert store.get("../../MEMORY") is None


def test_wait_for_returns_terminal_job(tmp_path: Path) -> None:
    service = NetworkRequestService(_settings(tmp_path))
    job = service.jobs.create("request-1")
    service.jobs.update(
        str(job["job_id"]),
        status="completed",
        result={"report": "done"},
    )

    waited = service.wait_for(str(job["job_id"]), 30)

    assert waited is not None
    assert waited["status"] == "completed"
    service.close()


def test_network_routes_question_action_and_capture(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = NetworkRequestService(_settings(tmp_path))

    class FakeProcessor:
        def __init__(self, intent: str) -> None:
            self.intent = intent

        def classify_text_intent(self, text: str) -> dict[str, str]:
            return {"intent": self.intent, "workflow": "test"}

        def answer_question(self, text: str, user_id: int) -> dict[str, Any]:
            return {"report": "question"}

        def execute_prompt(self, text: str, user_id: int) -> dict[str, Any]:
            return {"report": "action"}

    monkeypatch.setattr(service, "_capture", lambda text, source: {"report": "capture"})

    cases = (("question", "question"), ("action", "action"), ("capture", "capture"))
    for intent, expected in cases:
        monkeypatch.setattr(
            service,
            "_processor",
            lambda intent=intent: FakeProcessor(intent),
        )
        result = service._dispatch("request", f"job-{intent}")
        assert result["report"] == expected

    service.close()


def test_http_api_auth_job_and_artifact_download(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    settings.network_api_port = 0
    artifact = settings.vault_path / "attachments" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("result", encoding="utf-8")
    server = NetworkServer(settings)
    monkeypatch.setattr(
        server.requests,
        "_dispatch",
        lambda text, job_id: {"report": "done", "artifact_paths": [str(artifact)]},
    )
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    port = server.httpd.server_port

    try:
        connection = HTTPConnection("127.0.0.1", port)
        connection.request(
            "POST",
            "/v1/requests",
            body=json.dumps({"request_id": "r1", "text": "make file"}),
            headers={
                "Authorization": "Bearer network-token",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 202
        job_id = body["job_id"]

        connection.request(
            "POST",
            "/v1/requests",
            body=json.dumps({"request_id": "r1", "text": "different"}),
            headers={
                "Authorization": "Bearer network-token",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == 409
        assert json.loads(response.read())["error"] == (
            "request_id reused with different text"
        )

        connection.request(
            "GET",
            f"/v1/jobs/{job_id}?wait=5",
            headers={"Authorization": "Bearer network-token"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body["status"] == "completed"
        assert body["result"]["report"] == "done"
        artifact_url = body["result"]["artifacts"][0]["download_url"]

        connection.request(
            "GET",
            artifact_url,
            headers={"Authorization": "Bearer network-token"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"result"

        connection.request(
            "GET",
            f"/v1/jobs/{job_id}/artifacts/artifact--1",
            headers={"Authorization": "Bearer network-token"},
        )
        assert connection.getresponse().status == 404

        connection.request(
            "GET",
            "/v1/jobs/../../MEMORY",
            headers={"Authorization": "Bearer network-token"},
        )
        assert connection.getresponse().status == 404

        connection.request("GET", "/v1/jobs/unknown")
        assert connection.getresponse().status == 401
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_vault_tree_read_write_and_conflict(
    tmp_path: Path,
    write_vault_manifest: Any,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "projects").mkdir()
    (vault / ".session").mkdir()
    write_vault_manifest(vault)
    target = vault / "projects" / "note.md"
    target.write_bytes(b"original")
    settings = _settings(tmp_path)
    settings.vault_path = vault
    settings.network_api_port = 0
    server = NetworkServer(settings)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        connection.request("GET", "/v1/vault/tree?path=projects", headers=headers)
        response = connection.getresponse()
        tree = json.loads(response.read())
        assert response.status == 200
        assert tree["entries"][0]["path"] == "projects/note.md"
        assert tree["listing_complete"] is True

        connection.request(
            "GET", "/v1/vault/file?path=projects/note.md", headers=headers
        )
        response = connection.getresponse()
        etag = response.getheader("ETag")
        assert response.status == 200
        assert response.read() == b"original"

        connection.request(
            "GET",
            "/v1/vault/file?path=projects/note.md",
            headers={**headers, "If-None-Match": etag or ""},
        )
        response = connection.getresponse()
        assert response.status == 304
        assert response.getheader("ETag") == etag
        assert response.read() == b""

        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"updated",
            headers={
                **headers,
                "If-Match": etag or "",
                "X-Content-SHA256": "0" * 64,
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "body checksum mismatch"

        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"updated",
            headers={
                **headers,
                "If-Match": etag or "",
                "X-Content-SHA256": hashlib.sha256(b"updated").hexdigest(),
            },
        )
        response = connection.getresponse()
        assert response.status == 200

        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"stale-client",
            headers={**headers, "If-Match": etag or ""},
        )
        response = connection.getresponse()
        conflict = json.loads(response.read())
        assert response.status == 409
        assert conflict["error"] == "conflict"

        connection.request(
            "POST",
            f"/v1/vault/conflicts/{conflict['conflict_id']}/resolve",
            body=json.dumps({"decision": "replace"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert target.read_bytes() == b"stale-client"
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_compiled_file_rejects_outside_owner_notes(
    tmp_path: Path,
    write_vault_manifest: Any,
) -> None:
    vault = tmp_path / "vault"
    (vault / "compiled" / "topics").mkdir(parents=True)
    write_vault_manifest(vault, overrides={"user_content_roots": ["vault/compiled"]})
    target = vault / "compiled" / "topics" / "note.md"
    target.write_text(
        "---\ntype: topic\n---\n# Note\n\n## Owner Notes\n"
        "<!-- human:start -->\nold\n<!-- human:end -->\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.vault_path = vault
    settings.network_api_port = 0
    server = NetworkServer(settings)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        connection.request(
            "GET", "/v1/vault/file?path=compiled/topics/note.md", headers=headers
        )
        response = connection.getresponse()
        etag = response.getheader("ETag")
        original = response.read()
        proposed = original.replace(b"# Note", b"# Changed")

        connection.request(
            "PUT",
            "/v1/vault/file?path=compiled/topics/note.md",
            body=proposed,
            headers={**headers, "If-Match": etag or ""},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 422
        assert payload == {
            "error": "only owner notes may be edited",
            "reason": "outside_owner_notes",
        }
        assert target.read_bytes() == original
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_qmd_search_has_fixed_limit_and_paths(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    settings.network_api_port = 0
    server = NetworkServer(settings)
    called: dict[str, Any] = {}

    def fake_search(vault_path: Path, query: str, *, limit: int) -> FindSearchResult:
        called.update(vault_path=vault_path, query=query, limit=limit)
        hits = tuple(
            FindHit(
                rel_path=f"projects/project-{index}/note.md",
                title=f"Note {index}",
                snippet="snippet",
            )
            for index in range(limit)
        )
        return FindSearchResult(hits=hits, total=limit, limited=True)

    monkeypatch.setattr("d_brain.network_server.search_in_vault", fake_search)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        connection.request(
            "GET",
            "/v1/vault/search?q=project&limit=1",
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert called["query"] == "project"
        assert called["limit"] == 20
        assert len(payload["results"]) == 20
        assert payload["limited"] is True
        assert payload["results"][0]["directory"] == "projects/project-0"
        assert payload["results"][0]["name"] == "note.md"

        connection.request(
            "GET",
            "/v1/vault/search?q=",
            headers=headers,
        )
        assert connection.getresponse().status == 400
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_healthz_and_job_wait_validation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    settings.network_api_port = 0
    release = threading.Event()
    started = threading.Event()
    server = NetworkServer(settings)

    def fake_dispatch(text: str, job_id: str) -> dict[str, Any]:
        started.set()
        release.wait(timeout=5)
        return {"report": "finished", "artifact_paths": []}

    monkeypatch.setattr(server.requests, "_dispatch", fake_dispatch)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "ok"}

        connection.request(
            "POST",
            "/v1/requests",
            body=json.dumps({"request_id": "r-wait", "text": "slow task"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        job_id = json.loads(response.read())["job_id"]
        assert response.status == 202
        assert started.wait(timeout=2)

        for bad_wait in ("abc", "-1", "31"):
            connection.request(
                "GET",
                f"/v1/jobs/{job_id}?wait={bad_wait}",
                headers=headers,
            )
            response = connection.getresponse()
            assert response.status == 400
            response.read()

        def _release_soon() -> None:
            release.set()

        timer = threading.Timer(0.05, _release_soon)
        timer.start()
        try:
            connection.request(
                "GET",
                f"/v1/jobs/{job_id}?wait=5",
                headers=headers,
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["status"] == "completed"
            assert payload["result"]["report"] == "finished"
        finally:
            timer.cancel()
    finally:
        release.set()
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_put_validation_errors_and_conflict_cancel_and_double_edit(
    tmp_path: Path,
    write_vault_manifest: Any,
    monkeypatch: Any,
) -> None:
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_vault_manifest(vault)
    target = vault / "projects" / "note.md"
    target.write_bytes(b"v0")
    settings = _settings(tmp_path)
    settings.vault_path = vault
    settings.network_api_port = 0
    server = NetworkServer(settings)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        # PUT without If-Match -> 428
        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"v1",
            headers=headers,
        )
        response = connection.getresponse()
        assert response.status == 428
        response.read()

        # Read initial ETag
        connection.request(
            "GET", "/v1/vault/file?path=projects/note.md", headers=headers
        )
        response = connection.getresponse()
        etag0 = response.getheader("ETag") or ""
        response.read()

        # PUT without Content-Length -> 411
        connection.putrequest("PUT", "/v1/vault/file?path=projects/note.md")
        connection.putheader("Authorization", "Bearer network-token")
        connection.putheader("If-Match", etag0)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 411
        response.read()

        # PUT exceeding MAX_FILE_BYTES -> 413
        monkeypatch.setattr("d_brain.network_server.MAX_FILE_BYTES", 8)
        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"0123456789",
            headers={**headers, "If-Match": etag0},
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        monkeypatch.undo()

        # Conflict -> decision="cancel"
        target.write_bytes(b"v1")
        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"cancel-me",
            headers={**headers, "If-Match": etag0},
        )
        response = connection.getresponse()
        conflict_cancel = json.loads(response.read())
        assert response.status == 409

        connection.request(
            "POST",
            f"/v1/vault/conflicts/{conflict_cancel['conflict_id']}/resolve",
            body=json.dumps({"decision": "cancel"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "cancelled"}
        assert target.read_bytes() == b"v1"

        # Double modification scenario: no infinite 409 deadlock
        connection.request(
            "PUT",
            "/v1/vault/file?path=projects/note.md",
            body=b"client-final",
            headers={**headers, "If-Match": etag0},
        )
        response = connection.getresponse()
        first_conflict = json.loads(response.read())
        assert response.status == 409

        # File changes a second time before conflict is resolved
        target.write_bytes(b"v2")

        connection.request(
            "POST",
            f"/v1/vault/conflicts/{first_conflict['conflict_id']}/resolve",
            body=json.dumps({"decision": "replace"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        second_conflict = json.loads(response.read())
        assert response.status == 409
        assert second_conflict["conflict_id"] != first_conflict["conflict_id"]
        assert second_conflict["current_etag"] == hashlib.sha256(b"v2").hexdigest()

        # Resolving the fresh conflict succeeds
        connection.request(
            "POST",
            f"/v1/vault/conflicts/{second_conflict['conflict_id']}/resolve",
            body=json.dumps({"decision": "replace"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert target.read_bytes() == b"client-final"
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_create_file_and_decisions_and_lock_timeout(
    tmp_path: Path,
    write_vault_manifest: Any,
    monkeypatch: Any,
) -> None:
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    (vault / ".session").mkdir(parents=True)
    write_vault_manifest(vault)
    (vault / ".session" / "decisions-queue.json").write_text(
        json.dumps(
            [
                {
                    "kind": "fact-check-rejected",
                    "page": "compiled/topics/demo.md",
                    "summary": "Verify claim",
                    "since": "2026-11-28",
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.vault_path = vault
    settings.network_api_port = 0
    server = NetworkServer(settings)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        # POST /v1/vault/file -> 201
        connection.request(
            "POST",
            "/v1/vault/file",
            body=json.dumps({"path": "projects/new-note.md", "content": "# Hello\n"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        created = json.loads(response.read())
        assert response.status == 201
        assert created["path"] == "projects/new-note.md"
        assert created["size"] == len(b"# Hello\n")
        assert created["etag"] == hashlib.sha256(b"# Hello\n").hexdigest()
        assert (vault / "projects" / "new-note.md").read_text(encoding="utf-8") == (
            "# Hello\n"
        )

        # POST /v1/vault/file on existing file -> 409
        connection.request(
            "POST",
            "/v1/vault/file",
            body=json.dumps({"path": "projects/new-note.md", "content": "overwrite"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 409
        response.read()

        # POST /v1/vault/file in hidden folder -> 403
        connection.request(
            "POST",
            "/v1/vault/file",
            body=json.dumps({"path": ".session/leak.md", "content": "nope"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        # GET /v1/vault/decisions -> 200
        connection.request("GET", "/v1/vault/decisions", headers=headers)
        response = connection.getresponse()
        decisions = json.loads(response.read())
        assert response.status == 200
        assert decisions["total"] == 1
        assert decisions["items"][0]["kind"] == "fact-check-rejected"
        assert decisions["items"][0]["page"] == "compiled/topics/demo.md"

        # Lock timeout -> 503
        def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
            raise VaultLockTimeout("vault write lock is busy")

        monkeypatch.setattr(
            "d_brain.services.vault_file_service.VaultFileService.create_file",
            _raise_timeout,
        )
        connection.request(
            "POST",
            "/v1/vault/file",
            body=json.dumps({"path": "projects/busy.md", "content": "hi"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 503
        response.read()
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)


def test_http_artifact_blocks_hidden_dirs_and_enforces_size_limit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    settings.network_api_port = 0
    secret = settings.vault_path / ".session" / "secret.json"
    secret.parent.mkdir(parents=True)
    secret.write_text("top-secret", encoding="utf-8")
    big = settings.vault_path / "attachments" / "big.bin"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"0123456789abcdef")
    rel_ok = settings.vault_path / "attachments" / "ok.txt"
    rel_ok.write_bytes(b"ok")

    server = NetworkServer(settings)
    monkeypatch.setattr(
        server.requests,
        "_dispatch",
        lambda text, job_id: {
            "report": "done",
            "artifact_paths": [
                ".session/secret.json",
                "attachments/big.bin",
                "attachments/ok.txt",
            ],
        },
    )
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.httpd.server_port)
    headers = {"Authorization": "Bearer network-token"}

    try:
        connection.request(
            "POST",
            "/v1/requests",
            body=json.dumps({"request_id": "r-art", "text": "artifacts"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        job_id = json.loads(response.read())["job_id"]
        assert response.status == 202

        connection.request("GET", f"/v1/jobs/{job_id}?wait=5", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["status"] == "completed"

        # Hidden .session/ artifact is blocked (404)
        connection.request(
            "GET", f"/v1/jobs/{job_id}/artifacts/artifact-0", headers=headers
        )
        response = connection.getresponse()
        assert response.status == 404
        response.read()

        # Oversized artifact returns 413
        monkeypatch.setattr("d_brain.network_server.MAX_FILE_BYTES", 8)
        connection.request(
            "GET", f"/v1/jobs/{job_id}/artifacts/artifact-1", headers=headers
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()

        # Relative path under attachments/ resolves against vault_path and succeeds
        connection.request(
            "GET", f"/v1/jobs/{job_id}/artifacts/artifact-2", headers=headers
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"ok"
    finally:
        server.httpd.shutdown()
        server.requests.close()
        thread.join(timeout=2)
