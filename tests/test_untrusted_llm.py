"""Untrusted LLM calls: no tools that write or fetch, scratch cwd, no vault writes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from d_brain.services import cli_runner
from d_brain.services.cli_runner import CliRunner
from d_brain.services.documents import DocumentArchiveService, DocumentExtractionResult
from d_brain.services.image_analysis import ImageAnalysisService
from d_brain.services.link_summary import LinkSummaryService
from d_brain.services.plaud import PlaudSyncService


@pytest.fixture(autouse=True)
def _block_real_ai_cli() -> None:
    """This module tests ``CliRunner.run`` itself, so it keeps the real one."""


@pytest.fixture(autouse=True)
def _reset_untrusted_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The warning fires once per process, so each test starts with a clean slate."""
    monkeypatch.setattr(cli_runner, "_WARNED_UNTRUSTED_CLIS", set())


def _fake_popen(calls: list[dict[str, Any]], stdout: str) -> object:
    class _FakeProcess:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            calls.append({"cmd": cmd, "cwd": Path(kwargs["cwd"])})
            self.returncode = 0

        def __enter__(self) -> _FakeProcess:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            del input, timeout
            return stdout, ""

    return _FakeProcess


def _outside(path: Path, vault: Path) -> bool:
    return vault.resolve() not in [path.resolve(), *path.resolve().parents]


def test_run_untrusted_claude_denies_write_and_fetch_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, '{"type":"result","result":"ok"}\n'),
    )

    assert CliRunner(vault, "claude").run_untrusted("p", timeout=5) == "ok"

    cmd = calls[0]["cmd"]
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for tool in (
        "Bash", "PowerShell", "Task", "Agent", "WebFetch", "WebSearch",
        "Write", "Edit", "MultiEdit", "NotebookEdit",
    ):
        assert tool in denied
    # Read-type tools are denied too: the scratch cwd is not a boundary for
    # them (absolute paths still reach the vault and secrets).
    for tool in ("Read", "Glob", "Grep", "LS", "NotebookRead"):
        assert tool in denied
    assert _outside(calls[0]["cwd"], vault)
    assert not calls[0]["cwd"].exists()


def test_run_untrusted_claude_allow_read_keeps_write_and_fetch_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, '{"type":"result","result":"ok"}\n'),
    )

    runner = CliRunner(vault, "claude")
    assert runner.run_untrusted("p", timeout=5, allow_read=True) == "ok"

    cmd = calls[0]["cmd"]
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for tool in (
        "Bash", "PowerShell", "Task", "Agent", "WebFetch", "WebSearch",
        "Write", "Edit", "MultiEdit", "NotebookEdit",
    ):
        assert tool in denied
    for tool in ("Read", "Glob", "Grep", "LS"):
        assert tool not in denied


def test_untrusted_warning_logged_once_per_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen", _fake_popen([], "ok")
    )
    caplog.set_level(logging.WARNING, logger=cli_runner.logger.name)

    def call(backend: str) -> None:
        try:
            CliRunner(tmp_path, backend).run_untrusted("p", timeout=5)  # type: ignore[arg-type]
        except cli_runner.CliExecutionError:
            pass  # output decoding is not under test

    call("gemini")
    call("gemini")
    call("opencode")

    messages = [
        r.getMessage() for r in caplog.records if "no restricted mode" in r.getMessage()
    ]
    assert len(messages) == 2
    assert "gemini" in messages[0] and "opencode" in messages[1]


def test_run_untrusted_codex_uses_read_only_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, "not-json\n"),
    )

    try:
        CliRunner(vault, "codex").run_untrusted("p", timeout=5)
    except cli_runner.CliExecutionError:
        pass  # output decoding is not under test

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert not any("workspace-write" in part for part in cmd)
    assert _outside(calls[0]["cwd"], vault)


@pytest.mark.parametrize("backend", ["qwen", "gemini", "grok", "opencode"])
def test_run_untrusted_unsupported_cli_uses_scratch_cwd(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen", _fake_popen(calls, "ok")
    )

    with caplog.at_level(logging.WARNING):
        try:
            CliRunner(vault, backend).run_untrusted("p", timeout=5)  # type: ignore[arg-type]
        except cli_runner.CliExecutionError:
            pass  # output decoding is not under test

    assert _outside(calls[0]["cwd"], vault)
    assert "has no restricted mode" in caplog.text


@pytest.mark.parametrize("backend", ["kimi", "claude-tmux", "codex-tmux"])
def test_run_untrusted_external_runner_gets_scratch_workdir(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    seen: list[Path] = []

    def _fake(prompt, workdir, env, timeout):  # type: ignore[no-untyped-def]
        del prompt, env, timeout
        seen.append(workdir)
        return "ok"

    monkeypatch.setitem(cli_runner._EXTERNAL_RUNNERS, backend, _fake)

    assert CliRunner(vault, backend).run_untrusted("p", timeout=5) == "ok"  # type: ignore[arg-type]
    assert _outside(seen[0], vault)


def test_run_keeps_vault_cwd_for_nightly_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen", _fake_popen(calls, "ok")
    )

    try:
        CliRunner(vault, "qwen").run("p", timeout=5, restricted=True)
    except cli_runner.CliExecutionError:
        pass  # output decoding is not under test

    assert calls[0]["cwd"] == vault


def test_nightly_restricted_claude_still_allows_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, '{"type":"result","result":"ok"}\n'),
    )

    CliRunner(tmp_path, "claude").run("p", timeout=5, restricted=True)

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash,WebFetch,WebSearch"


def _record_run(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(
        self: CliRunner,
        prompt: str,
        *,
        timeout: int,
        extra_env: Any = None,
        restricted: bool = False,
        light: bool = False,
        cwd: Path | None = None,
        untrusted: bool = False,
        allow_read: bool = False,
    ) -> str:
        entry: dict[str, Any] = {
            "allow_read": allow_read,
            "prompt": prompt,
            "restricted": restricted,
            "untrusted": untrusted,
            "cwd": cwd,
        }
        if cwd is not None and cwd.exists():
            entry["files"] = {p.name: p.read_bytes() for p in cwd.iterdir()}
        seen.append(entry)
        return reply

    monkeypatch.setattr(CliRunner, "run", fake_run)
    return seen


def _assert_untrusted(entry: dict[str, Any], vault: Path) -> None:
    assert entry["restricted"] is True
    assert entry["untrusted"] is True
    assert entry["cwd"] is not None
    assert _outside(entry["cwd"], vault)


def test_link_summary_llm_call_is_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_run(monkeypatch, '{"summary":"s"}')
    service = LinkSummaryService(tmp_path / "vault", "qwen", "en")

    summary = service._llm_summary(title="t", url="https://example.com", content="c")
    assert summary == "s"
    _assert_untrusted(seen[0], tmp_path / "vault")
    assert seen[0]["allow_read"] is False


def test_youtube_summary_llm_call_is_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_run(monkeypatch, '{"summary":"s"}')
    service = LinkSummaryService(tmp_path / "vault", "qwen", "en")

    service._llm_youtube_summary(
        title="t", url="https://youtu.be/demo1234567", content="c"
    )
    _assert_untrusted(seen[0], tmp_path / "vault")


def test_document_summary_llm_call_is_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_run(monkeypatch, '{"summary":"s"}')
    service = DocumentArchiveService(tmp_path / "vault", ai_cli="qwen")
    extraction = DocumentExtractionResult(
        plain_text="body",
        title="Fictional report",
        format="text",
        warnings=[],
        metadata={},
        truncated=False,
        source_path="imports/documents/text/report.txt",
    )

    service._llm_summary(extraction)
    _assert_untrusted(seen[0], tmp_path / "vault")


def test_plaud_run_prompt_is_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_run(monkeypatch, "{}")
    vault = tmp_path / "vault"
    vault.mkdir()
    service = PlaudSyncService(vault, bearer_token="token", ai_cli="qwen")

    service._run_prompt("x")
    _assert_untrusted(seen[0], vault)


def test_image_analysis_is_untrusted_and_uses_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    image = vault / "attachments" / "d" / "img.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake-png")
    seen = _record_run(monkeypatch, '{"description":"d","ocr_text":"t"}')

    result = ImageAnalysisService(vault, "qwen").analyze("attachments/d/img.png")

    entry = seen[0]
    _assert_untrusted(entry, vault)
    assert entry["allow_read"] is True
    assert entry["files"] == {"image.png": b"fake-png"}
    assert str(entry["cwd"] / "image.png") in entry["prompt"]
    assert str(image) not in entry["prompt"]
    assert result == {"description": "d", "ocr_text": "t"}
    assert image.read_bytes() == b"fake-png"
    assert sorted(p.name for p in vault.rglob("*") if p.is_file()) == ["img.png"]
    assert not entry["cwd"].exists()
