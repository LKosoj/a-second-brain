import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain import run_compiled_import_sweep
from d_brain.services.compiled_briefings import (
    DEFAULT_QUEUE_BATCH_SIZE,
    NIGHTLY_IMPORT_SWEEP_LIMIT,
    CompiledSourceStateError,
)


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_import_sweep,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault, content_language="ru", ai_cli="claude"
        ),
    )


class FakeService:
    """Records every call instead of doing real queue/model/file work, like
    ``test_run_compiled_pass.py``'s ``FakeService`` for the sibling CLI."""

    created: list["FakeService"] = []

    def __init__(self, vault_path, content_language="ru", ai_cli=None) -> None:  # noqa: ANN001
        self.vault_path = Path(vault_path)
        self.content_language = content_language
        self.ai_cli = ai_cli
        self.calls: list[tuple[str, dict]] = []
        self.sweep_result: dict = {"marked_used": 0, "requeued": 0, "skipped": 0}
        self.sweep_error: Exception | None = None
        # Each entry is one drain_queue() call's return value, consumed in
        # order; the last entry repeats once the list is exhausted, so a
        # test only needs to spell out the rounds that actually differ.
        self.drain_results: list[dict] = [
            {"drained": 0, "updated": [], "consolidations": [], "errors": []}
        ]
        FakeService.created.append(self)

    def sweep_unmarked_imports(self, limit):  # noqa: ANN001
        self.calls.append(("sweep_unmarked_imports", {"limit": limit}))
        if self.sweep_error is not None:
            raise self.sweep_error
        return dict(self.sweep_result)

    def drain_queue(self, *, force, max_events):  # noqa: ANN001
        self.calls.append(("drain_queue", {"force": force, "max_events": max_events}))
        round_index = len([c for c in self.calls if c[0] == "drain_queue"]) - 1
        result = self.drain_results[min(round_index, len(self.drain_results) - 1)]
        return dict(result)


@pytest.fixture(autouse=True)
def _reset_fake_service():
    FakeService.created = []
    yield
    FakeService.created = []


@pytest.fixture(autouse=True)
def _stub_compiled_index_refresh(monkeypatch):
    """``_run_sweep`` best-effort-refreshes ``MOC/compiled-index.md`` (T3)
    through a local import; stubbed by default so every test here is
    isolated from that module's own vault-walking behavior unless a test
    overrides this to check the flag it returns."""
    monkeypatch.setattr(
        "d_brain.services.compiled_index.refresh_compiled_index",
        lambda vault_path, **kwargs: False,  # noqa: ARG005
    )


def test_default_mode_sweeps_with_nightly_limit_and_drains_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [
        ("sweep_unmarked_imports", {"limit": NIGHTLY_IMPORT_SWEEP_LIMIT}),
        (
            "drain_queue",
            {"force": True, "max_events": DEFAULT_QUEUE_BATCH_SIZE},
        ),
    ]
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == []
    assert out["compiled_index_written"] is False
    assert out["drain"]["drained"] == 0


def test_no_limit_flag_sweeps_with_unbounded_limit(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--no-limit"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls[0] == ("sweep_unmarked_imports", {"limit": None})


def test_drain_loop_repeats_until_zero_progress(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.drain_results = [
            {
                "drained": 2,
                "updated": ["compiled/topics/a.md"],
                "consolidations": [],
                "errors": [],
            },
            {
                "drained": 1,
                "updated": ["compiled/topics/b.md"],
                "consolidations": [],
                "errors": [],
            },
            {"drained": 0, "updated": [], "consolidations": [], "errors": []},
        ]
        return service

    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 0
    service = FakeService.created[0]
    drain_calls = [call for call in service.calls if call[0] == "drain_queue"]
    assert len(drain_calls) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["drain"]["drained"] == 3
    assert out["drain"]["updated"] == ["compiled/topics/a.md", "compiled/topics/b.md"]
    assert out["drain"]["queue_busy"] is False


def test_drain_loop_stops_immediately_on_worker_busy(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.drain_results = [
            {
                "drained": 0,
                "updated": [],
                "consolidations": [],
                "errors": ["worker-busy"],
            }
        ]
        return service

    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 1
    service = FakeService.created[0]
    drain_calls = [call for call in service.calls if call[0] == "drain_queue"]
    assert len(drain_calls) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == ["worker-busy"]
    assert out["drain"]["queue_busy"] is True


def test_compiled_source_state_error_becomes_json_error_not_a_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.sweep_error = CompiledSourceStateError(
            "unsupported compiled source state version"
        )
        return service

    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"]
    assert "источник" in out["errors"][0]


def test_compiled_index_written_flag_is_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_import_sweep, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(
        "d_brain.services.compiled_index.refresh_compiled_index",
        lambda vault_path, **kwargs: True,  # noqa: ARG005
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_import_sweep.main()

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["compiled_index_written"] is True
