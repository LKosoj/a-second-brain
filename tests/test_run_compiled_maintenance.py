import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain import run_compiled_maintenance
from d_brain.services.compiled_briefings import CompiledSourceStateError


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_maintenance,
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
        self.queue_worker_result: dict = {
            "drained": 0,
            "updated": [],
            "consolidations": [],
            "errors": [],
        }
        self.nightly_result: dict = {"queued_drained": 0, "errors": []}
        # The real ``initialize_source_state`` always returns all six keys,
        # ``errors`` included and empty (code review). A shorter fake would
        # let a test assert ``"errors" not in out`` and pass against a
        # contract production never honors.
        self.initialize_result: dict = {
            "evaluated": 0,
            "initialized": 0,
            "unchanged": 0,
            "changed": 0,
            "removed": [],
            "errors": [],
        }
        self.lint_issues: list[str] = []
        self.freshness: list[str] = []
        self.nightly_error: Exception | None = None
        self.queue_worker_error: Exception | None = None
        FakeService.created.append(self)

    def run_queue_worker(self, *, force, max_events):  # noqa: ANN001
        self.calls.append(
            ("run_queue_worker", {"force": force, "max_events": max_events})
        )
        if self.queue_worker_error is not None:
            raise self.queue_worker_error
        return dict(self.queue_worker_result)

    def run_nightly_maintenance(self):
        self.calls.append(("run_nightly_maintenance", {}))
        if self.nightly_error is not None:
            raise self.nightly_error
        return dict(self.nightly_result)

    def initialize_source_state(self):
        self.calls.append(("initialize_source_state", {}))
        return dict(self.initialize_result)

    def lint_notes(self):
        self.calls.append(("lint_notes", {}))
        return list(self.lint_issues)

    def freshness_issues(self):
        self.calls.append(("freshness_issues", {}))
        return list(self.freshness)


@pytest.fixture(autouse=True)
def _reset_fake_service():
    FakeService.created = []
    yield
    FakeService.created = []


def test_maintenance_default_mode_calls_run_queue_worker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [
        ("run_queue_worker", {"force": False, "max_events": 8})
    ]
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == []


def test_maintenance_nightly_flag_calls_run_nightly_maintenance(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--nightly"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [("run_nightly_maintenance", {})]


def test_maintenance_initialize_source_state_builds_a_baseline_and_validates_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The third mode: build the freshness baseline, then immediately lint it
    and check freshness. Neither the queue drain nor the nightly pass may
    run in this mode -- the whole point is a baseline "without rewriting
    notes"."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--initialize-source-state"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [
        ("initialize_source_state", {}),
        ("lint_notes", {}),
        ("freshness_issues", {}),
    ]
    out = json.loads(capsys.readouterr().out)
    assert out["initialized"] == 0
    assert out["lint_issues"] == []
    assert out["freshness_issues"] == []
    # ``initialize_source_state`` reports an empty ``errors`` list rather
    # than omitting the key, so "clean run" is an empty list, not a missing
    # one -- and ``main`` maps exactly that to exit 0.
    assert out["errors"] == []


def test_maintenance_initialize_source_state_fails_when_the_baseline_is_dirty(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A baseline that already lints dirty is not a baseline: the run must
    exit non-zero and say so, or the operator would take a broken starting
    state for a good one and every later freshness verdict would be built
    on top of it."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.freshness = ["compiled/topics/aurora.md: stale source"]
        return service

    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--initialize-source-state"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == ["source-state-validation-failed"]
    assert out["freshness_issues"] == ["compiled/topics/aurora.md: stale source"]


def test_maintenance_queue_only_corrupt_source_state_yields_clean_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Code review Finding 2: the ordinary queue-only run (the CLI's
    default mode, no flags) used to have no try/except at all around the
    service call -- a corrupt ``.compiled/source-state.json`` would crash
    ``main()`` with a raw Python traceback instead of the CLI's normal JSON
    ``{"errors": [...]}`` contract. This must still exit non-zero (it is a
    real failure), but with a readable message and no uncaught exception."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.queue_worker_error = CompiledSourceStateError(
            "invalid compiled source state: /vault/.compiled/source-state.json"
        )
        return service

    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"]
    assert "источник" in out["errors"][0]


def test_maintenance_nightly_corrupt_source_state_still_fails_with_clean_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Same clean-message handling for ``--nightly``: corruption there is
    still a real failure (``run_nightly_maintenance`` already recorded it
    as a failed pass in its own journal before re-raising), it just should
    not surface as a raw traceback either."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.nightly_error = CompiledSourceStateError(
            "unsupported compiled source state version"
        )
        return service

    monkeypatch.setattr(
        run_compiled_maintenance, "CompiledBriefingService", _make_service
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--nightly"])

    exit_code = run_compiled_maintenance.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"]
    assert "источник" in out["errors"][0]
