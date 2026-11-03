"""Tests for durable network-job replay semantics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from d_brain.config import Settings
from d_brain.services.network_requests import (
    NetworkJobStore,
    NetworkRequestError,
    NetworkRequestService,
)


def _settings(vault: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="telegram",
        owner_telegram_id=1,
        network_api_token="network-token",
        network_api_host="127.0.0.1",
        network_api_port=8765,
        vault_path=vault,
    )


def test_submit_rejects_reused_request_id_with_different_text(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = NetworkRequestService(_settings(tmp_path / "vault"))
    monkeypatch.setattr(service, "_schedule", lambda job_id, text: None)

    first = service.submit("request-1", "первый текст")
    second = service.submit("request-1", "первый текст")

    assert second["job_id"] == first["job_id"]
    assert second["text_sha256"] == first["text_sha256"]
    with pytest.raises(
        NetworkRequestError, match="request_id reused with different text"
    ):
        service.submit("request-1", "другой текст")
    assert service.get(str(first["job_id"]))["text"] == "первый текст"  # type: ignore[index]
    service.close()


def test_restart_marks_interrupted_side_effect_as_failed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = NetworkJobStore(vault)
    job = store.create("request-1")
    store.update(
        str(job["job_id"]),
        text="запись",
        text_sha256="digest",
        status="running",
        side_effect_started_at="2026-11-03T10:00:00+03:00",
    )

    service = NetworkRequestService(_settings(vault))
    interrupted = service.get(str(job["job_id"]))

    assert interrupted is not None
    assert interrupted["status"] == "failed"
    assert interrupted["error"] == "interrupted"
    assert interrupted["retryable"] is False
    service.close()


def test_dispatch_marks_action_before_processor_call(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = NetworkRequestService(_settings(tmp_path / "vault"))
    job = service.jobs.create("request-1")
    service.jobs.update(str(job["job_id"]), text="поручение", status="queued")
    observed: list[str | None] = []

    class FakeProcessor:
        def classify_text_intent(self, text: str) -> dict[str, str]:
            return {"intent": "action", "workflow": "test"}

        def execute_prompt(self, text: str, user_id: int) -> dict[str, Any]:
            current = service.get(str(job["job_id"]))
            observed.append(
                current.get("side_effect_started_at") if current else None
            )
            return {"report": "done", "processed_entries": 0}

    monkeypatch.setattr(service, "_processor", lambda: FakeProcessor())
    monkeypatch.setattr(
        "d_brain.services.session.SessionStore.append",
        lambda *args, **kwargs: None,
    )

    service._dispatch("поручение", str(job["job_id"]))

    assert observed and observed[0]
    service.close()
