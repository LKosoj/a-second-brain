"""Regression tests for the G1 "cli-runner" audit fixes.

Covers: environment allowlisting for AI CLI subprocesses (cli_runner and
todoist_projects), a bounded ``communicate()`` after a timed-out CLI,
conservative quota-message detection, a timeout on the Todoist
``find-projects`` call, and the claude-tmux stall watchdog / transcript
cleanup.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from d_brain.services import claude_tmux
from d_brain.services.claude_tmux import run_claude_tmux
from d_brain.services.cli_runner import (
    CliExecutionError,
    CliRunner,
    build_subprocess_env,
    detect_terminal_backend_message,
)
from d_brain.services.plaud import PLAUD_TASK_TIMEOUT
from d_brain.services.processor import CliProcessor
from d_brain.services.todoist_projects import TodoistProjectCatalog

_COMPOSER_SCREEN = "\n❯ Try \"refactor <filepath>\"\n  ⏵⏵ bypass permissions on\n"


@pytest.fixture(autouse=True)
def _block_real_ai_cli() -> None:
    """This module tests ``CliRunner.run`` itself, so it keeps the real one."""


# --- A) environment allowlist -------------------------------------------


def test_build_subprocess_env_drops_unlisted_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shh")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "shh2")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-cfg")

    env = build_subprocess_env({"EXTRA_SECRET": "value"})

    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "DEEPGRAM_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-cfg"
    assert env["EXTRA_SECRET"] == "value"
    assert env["TERM"] == "dumb"
    assert env["NO_COLOR"] == "1"


def test_build_subprocess_env_keeps_gemini_and_grok_auth_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini/Grok authenticate through GOOGLE_*/XAI_API_KEY, not GEMINI_*/GROK_*."""

    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    monkeypatch.setenv("XAI_API_KEY", "x-key")

    env = build_subprocess_env()

    assert env["GOOGLE_API_KEY"] == "g-key"
    assert env["XAI_API_KEY"] == "x-key"


def test_runner_run_does_not_leak_bot_token_to_popen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    captured_env: dict[str, str] = {}

    def fake_popen(*args: object, **kwargs: object) -> None:
        del args
        captured_env.update(kwargs["env"])  # type: ignore[arg-type]
        raise OSError(2, "not found")

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.Popen", fake_popen)
    runner = CliRunner(tmp_path, "qwen")

    with pytest.raises(CliExecutionError):
        runner.run("hello", timeout=5, extra_env={"MY_SECRET": "42"})

    assert "TELEGRAM_BOT_TOKEN" not in captured_env
    assert captured_env["PATH"] == "/usr/bin:/bin"
    assert captured_env["MY_SECRET"] == "42"


def test_todoist_refresh_env_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    (vault_path.parent / "mcp-config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    catalog = TodoistProjectCatalog(vault_path, todoist_api_key="todoist-secret")
    captured: dict[str, str] = {}

    def fake_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(cmd, 0, '{"projects": []}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    catalog.refresh()

    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["TODOIST_API_KEY"] == "todoist-secret"
    assert captured["PATH"] == "/usr/bin"
    assert "MCP_CONFIG_PATH" in captured


def test_processor_create_todoist_tasks_env_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")

    processor = CliProcessor(vault_path, todoist_api_key="todoist-secret")
    processor._project_catalog.get_catalog = (  # type: ignore[method-assign]
        lambda *, force_refresh=False: {
            "available": False,
            "catalog": None,
            "errors": [],
        }
    )
    captured: dict[str, str] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 0, '{"tasks": []}', "")

    monkeypatch.setattr("d_brain.services.processor.subprocess.run", fake_run)

    processor._create_todoist_tasks([{"content": "Buy milk"}])

    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["TODOIST_API_KEY"] == "todoist-secret"
    assert captured["PATH"] == "/usr/bin"
    assert captured["NPM_CONFIG_CACHE"].endswith("vault/.session/npm-cache")


def test_run_uv_script_env_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")

    processor = CliProcessor(vault_path)
    captured: dict[str, str] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("d_brain.services.processor.subprocess.run", fake_run)

    processor._run_uv_script("--version")

    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["PATH"] == "/usr/bin"


# --- B) bounded communicate() after a timed-out CLI ----------------------


class _FakeStuckProcess:
    """A CLI whose grandchild keeps holding the stdout pipe open."""

    def __init__(self) -> None:
        # Not a real pid/pgid on this machine, so `_stop_process_group`
        # takes its `os.getpgid` OSError branch deterministically.
        self.pid = 999_999_999
        self.returncode = None
        self.kill_calls = 0
        self.communicate_timeouts: list[float | None] = []

    def __enter__(self) -> _FakeStuckProcess:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) < 3:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return "stdout", "stderr"

    def kill(self) -> None:
        self.kill_calls += 1


def test_run_recovers_when_grandchild_holds_stdout_pipe_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the fix, the recovery `communicate()` call has no timeout and
    would either hang forever or let `subprocess.TimeoutExpired` escape
    instead of the documented `TimeoutError`."""

    fake_proc = _FakeStuckProcess()
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        lambda *args, **kwargs: fake_proc,
    )
    runner = CliRunner(Path("."), "qwen")

    with pytest.raises(TimeoutError):
        runner.run("hello", timeout=10)

    # main call, bounded grace-drain call, final untimed drain call.
    assert len(fake_proc.communicate_timeouts) == 3
    assert fake_proc.kill_calls == 2


def test_run_times_out_cleanly_when_grace_drain_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: the grace-bounded drain after the timeout succeeds,
    so the extra `proc.kill()` fallback in the except block is never needed
    (the one call below comes from `_stop_process_group`'s own OSError
    branch, since the fake pid is not a real process group)."""

    class _FakeProcess(_FakeStuckProcess):
        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            del input
            self.communicate_timeouts.append(timeout)
            if len(self.communicate_timeouts) == 1:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
            return "stdout", "stderr"

    fake_proc = _FakeProcess()
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        lambda *args, **kwargs: fake_proc,
    )
    runner = CliRunner(Path("."), "qwen")

    with pytest.raises(TimeoutError):
        runner.run("hello", timeout=10)

    assert len(fake_proc.communicate_timeouts) == 2
    assert fake_proc.kill_calls == 1


class _FakeStream:
    """Stand-in for a Popen pipe, tracking whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeUnkillableProcess:
    """A CLI whose grandchild keeps holding the pipes open even after SIGKILL."""

    def __init__(self) -> None:
        self.pid = 999_999_999
        self.returncode = None
        self.kill_calls = 0
        self.communicate_calls = 0
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    def __enter__(self) -> _FakeUnkillableProcess:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        self.communicate_calls += 1
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def kill(self) -> None:
        self.kill_calls += 1


def test_run_gives_up_draining_when_process_stays_stuck_after_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If even the post-kill drain times out, `run()` must still raise
    `TimeoutError` and close the pipes instead of hanging forever."""

    fake_proc = _FakeUnkillableProcess()
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        lambda *args, **kwargs: fake_proc,
    )
    runner = CliRunner(Path("."), "qwen")

    with pytest.raises(TimeoutError):
        runner.run("hello", timeout=10)

    # main call, bounded grace-drain call, bounded post-kill drain call.
    assert fake_proc.communicate_calls == 3
    # one from `_stop_process_group`'s OSError branch, one from the fallback.
    assert fake_proc.kill_calls == 2
    assert fake_proc.stdout.closed
    assert fake_proc.stderr.closed


# --- C) conservative quota-message detection ------------------------------


def test_detect_terminal_backend_message_ignores_long_quota_explanation() -> None:
    message = (
        "Quota exceeded means that you have used all of your allotted "
        "requests for the current billing period. It resets automatically "
        "at the start of the next cycle, so no action is needed on your "
        "part right now."
    )
    assert len(message) > 200  # sanity: this must exercise the long-text path

    assert detect_terminal_backend_message(message) is None


def test_detect_terminal_backend_message_recognizes_short_quota_error() -> None:
    message = "Error: quota exceeded"

    assert detect_terminal_backend_message(message) == message


def test_detect_terminal_backend_message_recognizes_long_raw_quota_error() -> None:
    """A long message is still a raw backend error, not prose, when the
    marker is in its very first sentence and it starts with "error"."""

    message = (
        "Error: quota exceeded. Your request was rejected because the "
        "service quota exceeded its configured limit for this billing "
        "cycle, and no further calls will be accepted until it resets. "
        "Contact your administrator to request a higher limit."
    )
    assert len(message) > 200  # sanity: this must exercise the long-text path

    assert detect_terminal_backend_message(message) == message


def test_detect_terminal_backend_message_ignores_429_mentioned_later() -> None:
    """A "429" appearing later in the text must not make an unrelated,
    earlier "quota exceeded" mention terminal -- both signals have to share
    the same first sentence/line."""

    message = (
        "To handle a 429 response from the API, the client should apply "
        "exponential backoff and retry after the suggested delay. "
        "Separately, note that a quota exceeded condition is unrelated to "
        "rate limiting and requires a different remediation, namely "
        "raising your plan's monthly allocation with the provider."
    )
    assert len(message) > 200  # sanity: this must exercise the long-text path

    assert detect_terminal_backend_message(message) is None


def test_detect_terminal_backend_message_recognizes_error_with_429_code() -> None:
    message = "Error: quota exceeded (429)"

    assert detect_terminal_backend_message(message) == message


def _make_fake_popen(
    *, stdout: str, stderr: str = "", returncode: int = 0
) -> object:
    """A minimal ``subprocess.Popen`` replacement for a single ``run()`` call."""

    class _FakeProcess:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.returncode = returncode

        def __enter__(self) -> _FakeProcess:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            del input, timeout
            return stdout, stderr

    return _FakeProcess


def test_run_treats_long_stderr_quota_text_as_terminal_regardless_of_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stderr is never run through the answer-text quota heuristic: a
    non-zero exit always raises, no matter how the quota text reads."""

    long_stderr = (
        "Backend diagnostics: after retrying several times, the request "
        "could not be completed. It turns out that the account's quota "
        "exceeded its configured limit partway through this explanation, "
        "so no further detail is available at this time."
    )
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _make_fake_popen(stdout="", stderr=long_stderr, returncode=1),
    )
    runner = CliRunner(Path("."), "codex")

    with pytest.raises(CliExecutionError, match="quota exceeded"):
        runner.run("hello", timeout=10)


def test_run_treats_structured_json_quota_error_as_terminal_regardless_of_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured ``is_error`` result is never run through the answer-text
    quota heuristic either, even with a long explanatory message and a
    successful (0) exit code."""

    long_error = (
        "The service quota exceeded its configured limit for this billing "
        "cycle. This message explains what happened in several sentences "
        "so that the operator understands there is nothing else to retry "
        "right now and should wait for the next billing period."
    )
    stream = json.dumps({"type": "result", "is_error": True, "result": long_error})
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _make_fake_popen(stdout=stream, stderr="", returncode=0),
    )
    runner = CliRunner(Path("."), "qwen")

    with pytest.raises(CliExecutionError, match="quota exceeded"):
        runner.run("hello", timeout=10)


# --- D) Todoist find-projects timeout -------------------------------------


def test_todoist_refresh_wraps_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    catalog = TodoistProjectCatalog(vault_path, todoist_api_key="token")
    captured_timeout: list[float | None] = []

    def fake_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_timeout.append(kwargs.get("timeout"))  # type: ignore[arg-type]
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        catalog.refresh()

    assert captured_timeout == [120]


def test_todoist_get_catalog_reports_timeout_as_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    catalog = TodoistProjectCatalog(vault_path, todoist_api_key="token")

    def fake_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = catalog.get_catalog(force_refresh=True)

    assert result["available"] is False
    assert any("timed out" in err for err in result["errors"])


# --- E) claude-tmux stall watchdog and transcript cleanup -----------------


class _FakeTmuxForFixTests:
    """Minimal tmux stand-in for the stall-watchdog and cleanup tests."""

    def __init__(
        self,
        home: Path,
        *,
        complete_turn: bool = True,
        include_answer: bool = True,
    ) -> None:
        self.home = home
        self.complete_turn = complete_turn
        self.include_answer = include_answer
        self.session_id = ""
        self.pasted_prompt = ""
        self.kill_server_calls = 0

    def _transcript_dir(self) -> Path:
        path = self.home / ".claude" / "projects" / "-fake-work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def transcript_path(self) -> Path:
        return self._transcript_dir() / f"{self.session_id}.jsonl"

    def _write_turn(self) -> None:
        records: list[dict[str, object]] = [
            {"type": "user", "message": {"content": self.pasted_prompt}}
        ]
        if self.complete_turn:
            message: dict[str, object] = {"stop_reason": "end_turn"}
            message["content"] = (
                [{"type": "text", "text": "answer text"}]
                if self.include_answer
                else []
            )
            records.append({"type": "assistant", "message": message})
            records.append({"type": "system", "subtype": "turn_duration"})
        self.transcript_path().write_text(
            "\n".join(json.dumps(record) for record in records),
            encoding="utf-8",
        )

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        args = list(cmd[3:])
        stdout = ""
        if args[0] == "new-session":
            self.session_id = args[args.index("--session-id") + 1]
            self._transcript_dir()
        elif args[0] == "capture-pane":
            stdout = _COMPOSER_SCREEN
        elif args[0] == "list-panes":
            stdout = "0 \n"
        elif args[0] == "load-buffer":
            self.pasted_prompt = str(kwargs.get("input") or "")
        elif args[0] == "send-keys" and args[-1] == "Enter" and self.pasted_prompt:
            self._write_turn()
        elif args[0] == "kill-server":
            self.kill_server_calls += 1
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_tmux, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(claude_tmux, "_PASTE_SETTLE", 0.0)
    monkeypatch.setattr(claude_tmux, "_COMPOSER_TIMEOUT", 1.0)
    monkeypatch.setattr(claude_tmux, "_PROMPT_ACCEPT_TIMEOUT", 1.0)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeTmuxForFixTests) -> None:
    monkeypatch.setattr(claude_tmux.subprocess, "run", fake)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_default_stall_seconds_exceeds_plaud_task_timeout() -> None:
    """The transcript only grows once an assistant block finishes, so the
    default idle bound must clear the longest known tool-free call (plaud's
    own task timeout) or it would kill a legitimate long, silent turn early."""

    assert claude_tmux._stall_seconds({}) > PLAUD_TASK_TIMEOUT


def test_wait_for_answer_stall_watchdog_fires_before_full_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that never progresses past the initial prompt must fail long
    before the caller's generous overall timeout (30s here)."""

    _fast_polling(monkeypatch)
    home = tmp_path / "home"
    fake = _FakeTmuxForFixTests(home, complete_turn=False)
    _install(monkeypatch, fake)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="progress"):
        run_claude_tmux(
            "prompt",
            tmp_path / "work",
            {"HOME": str(home), "CLAUDE_TMUX_STALL_SECONDS": "0.1"},
            30,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert fake.kill_server_calls  # cleaned up via the outer finally


def test_run_claude_tmux_deletes_transcript_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_polling(monkeypatch)
    home = tmp_path / "home"
    fake = _FakeTmuxForFixTests(home, complete_turn=True, include_answer=True)
    _install(monkeypatch, fake)

    output = run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert output == "answer text"
    assert not fake.transcript_path().exists()


def test_run_claude_tmux_keeps_transcript_after_failed_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_polling(monkeypatch)
    home = tmp_path / "home"
    fake = _FakeTmuxForFixTests(home, complete_turn=True, include_answer=False)
    _install(monkeypatch, fake)

    with pytest.raises(claude_tmux.ClaudeTmuxError, match="without any text"):
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert fake.transcript_path().exists()
