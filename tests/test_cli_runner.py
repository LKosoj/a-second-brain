from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from d_brain.services import claude_tmux, cli_runner, todoist_projects
from d_brain.services.claude_tmux import run_claude_tmux
from d_brain.services.cli_json_stream import (
    recover_cli_error_from_raw_stream,
    recover_cli_text_from_raw_stream,
)
from d_brain.services.cli_runner import (
    CliExecutionError,
    CliRunner,
    build_subprocess_env,
    detect_terminal_backend_message,
    normalize_ai_cli,
)
from d_brain.services.plaud import PLAUD_TASK_TIMEOUT
from d_brain.services.processor import CliProcessor
from d_brain.services.todoist_projects import TodoistProjectCatalog


@pytest.fixture(autouse=True)
def _block_real_ai_cli() -> None:
    """This module tests ``CliRunner.run`` itself, so it keeps the real one."""


def _fake_popen(
    calls: list[dict[str, object]],
    *,
    stdout: str,
    stderr: str = "",
    returncode: int = 0,
):
    """Build a subprocess.Popen replacement that records how it was called."""

    class _FakeProcess:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            calls.append({"args": args, "kwargs": kwargs})
            self.returncode = returncode

        def __enter__(self) -> _FakeProcess:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            del timeout
            calls[-1]["input"] = input
            return stdout, stderr

    return _FakeProcess


def _process_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
    except FileNotFoundError:
        return False
    state = stat[stat.rfind(")") + 2]
    return state != "Z"


@pytest.mark.parametrize("backend", list(cli_runner.CLI_SPECS))
def test_light_model_rejection_retries_default_for_every_backend(
    tmp_path, monkeypatch, backend
):
    attempts = []
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")

    def run_once(self, prompt, **kwargs):
        attempts.append((prompt, kwargs))
        if kwargs.get("model"):
            raise CliExecutionError("The 'test-mini' model is not supported")
        return "default answer"

    monkeypatch.setattr(CliRunner, "_run_once", run_once)
    runner = CliRunner(tmp_path, backend)
    assert runner.run("question", timeout=30, light=True, restricted=True) == (
        "default answer"
    )
    assert [call[1].get("model") for call in attempts] == ["test-mini", None]
    assert all(call[0] == "question" for call in attempts)
    assert all(call[1]["restricted"] for call in attempts)
    assert all(0 < call[1]["timeout"] <= 30 for call in attempts)


@pytest.mark.parametrize("error", ["Unauthorized", "Network error", "quota exceeded"])
def test_light_model_does_not_retry_unrelated_errors(tmp_path, monkeypatch, error):
    attempts = []
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")

    def run_once(self, prompt, **kwargs):
        attempts.append(kwargs)
        raise CliExecutionError(error)

    monkeypatch.setattr(CliRunner, "_run_once", run_once)
    with pytest.raises(CliExecutionError, match=error):
        CliRunner(tmp_path, "codex").run("question", timeout=30, light=True)
    assert len(attempts) == 1


def test_light_model_success_needs_one_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")
    calls = []
    monkeypatch.setattr(
        cli_runner.subprocess, "Popen",
        _fake_popen(calls, stdout='{"type":"result","result":"answer"}\n'),
    )
    assert (
        CliRunner(tmp_path, "claude").run("question", timeout=30, light=True)
        == "answer"
    )
    assert calls[0]["args"][0][1:3] == ["--model", "test-mini"]
    assert calls[0]["input"] == "question"
    assert len(calls) == 1


@pytest.mark.parametrize("backend", ["kimi", "claude-tmux", "codex-tmux"])
def test_light_model_is_passed_to_external_runner(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")
    calls = []

    def run(prompt, workdir, env, timeout, *, model=None):
        calls.append(model)
        if model:
            raise RuntimeError("unknown model test-mini")
        return "answer"

    monkeypatch.setitem(cli_runner._EXTERNAL_RUNNERS, backend, run)
    assert (
        CliRunner(tmp_path, backend).run("question", timeout=30, light=True)
        == "answer"
    )
    assert calls == ["test-mini", None]


def test_absent_light_model_runs_default_without_model_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        cli_runner.subprocess, "Popen",
        _fake_popen(calls, stdout='{"type":"result","result":"answer"}\n'),
    )
    CliRunner(tmp_path, "claude").run("question", timeout=30, light=True)
    assert "--model" not in calls[0]["args"][0]
    assert len(calls) == 1


@pytest.mark.parametrize("returncode", [0, 1])
def test_structured_model_error_retries_without_override(
    tmp_path, monkeypatch, returncode
):
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")
    calls = []
    failed = _fake_popen(
        calls, returncode=returncode,
        stdout='{"type":"turn.failed","error":{"message":"model_not_found"}}\n',
    )
    succeeded = _fake_popen(
        calls,
        stdout='{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"answer"}}\n',
    )
    processes = iter([failed, succeeded])
    monkeypatch.setattr(
        cli_runner.subprocess, "Popen", lambda *a, **k: next(processes)(*a, **k)
    )
    assert (
        CliRunner(tmp_path, "codex").run("question", timeout=30, light=True)
        == "answer"
    )
    assert "--model" in calls[0]["args"][0]
    assert "--model" not in calls[1]["args"][0]


def test_default_model_failure_is_returned_without_a_third_attempt(
    tmp_path, monkeypatch
):
    attempts = []
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: "test-mini")

    def run_once(self, prompt, **kwargs):
        attempts.append(kwargs.get("model"))
        if kwargs.get("model"):
            raise CliExecutionError("unknown model")
        raise CliExecutionError("default model unavailable")

    monkeypatch.setattr(CliRunner, "_run_once", run_once)
    with pytest.raises(CliExecutionError, match="default model unavailable"):
        CliRunner(tmp_path, "codex").run("question", timeout=30, light=True)
    assert attempts == ["test-mini", None]


@pytest.mark.parametrize(
    "backend", ["claude", "codex", "qwen", "gemini", "grok", "opencode"]
)
def test_explicit_model_precedes_prompt_flags(backend):
    command = CliRunner(Path("."), backend).build_command("question", model="test-mini")
    assert command[1:3] == ["--model", "test-mini"]
    assert command[-1] == "question"


@pytest.mark.parametrize(
    "value",
    [
        "claude",
        "claude-tmux",
        "codex",
        "codex-tmux",
        "qwen",
        "gemini",
        "kimi",
        "grok",
        "opencode",
        "CLAUDE",
        " QWEN ",
    ],
)
def test_normalize_ai_cli_accepts_supported_values(value: str) -> None:
    normalized = normalize_ai_cli(value)
    assert normalized in {
        "claude",
        "claude-tmux",
        "codex",
        "codex-tmux",
        "qwen",
        "gemini",
        "kimi",
        "grok",
        "opencode",
    }


def test_normalize_ai_cli_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        normalize_ai_cli("unknown")


@pytest.mark.parametrize(
    ("backend", "expected_prefix"),
    [
        (
            "claude",
            [
                "claude",
                "-p",
                "--verbose",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
            ],
        ),
        (
            "codex",
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ],
        ),
        (
            "qwen",
            [
                "qwen",
                "--approval-mode",
                "yolo",
                "--output-format",
                "stream-json",
                "--prompt",
            ],
        ),
        (
            "gemini",
            [
                "gemini",
                "--approval-mode",
                "yolo",
                "--output-format",
                "stream-json",
                "-p",
            ],
        ),
        (
            "grok",
            [
                "grok",
                "--no-auto-update",
                "--always-approve",
                "--no-memory",
                "--output-format",
                "streaming-json",
                "-p",
            ],
        ),
        (
            "opencode",
            ["opencode", "run", "--format", "json"],
        ),
    ],
)
def test_build_command_uses_backend_specific_prefix(
    backend: str,
    expected_prefix: list[str],
) -> None:
    runner = CliRunner(Path("."), backend)
    command = runner.build_command("hello")
    assert command[:-1] == expected_prefix
    assert command[-1] == "hello"


def test_build_command_never_puts_kimi_prompt_in_argv() -> None:
    runner = CliRunner(Path("."), "kimi")

    assert runner.build_command("secret prompt") == ["kimi", "acp"]


def test_claude_tmux_delegates_to_the_tmux_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path, int]] = []

    def _fake_runner(prompt, workdir, env, timeout):  # type: ignore[no-untyped-def]
        del env
        calls.append((prompt, workdir, timeout))
        return "  tmux answer  "

    monkeypatch.setitem(cli_runner._EXTERNAL_RUNNERS, "claude-tmux", _fake_runner)
    runner = CliRunner(tmp_path, "claude-tmux")

    output = runner.run("secret prompt", timeout=42)

    assert output == "  tmux answer  "
    assert calls == [("secret prompt", tmp_path, 42)]
    assert runner.build_command("secret prompt") == [
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_codex_tmux_delegates_to_the_tmux_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path, int]] = []

    def _fake_runner(prompt, workdir, env, timeout):  # type: ignore[no-untyped-def]
        del env
        calls.append((prompt, workdir, timeout))
        return "tmux answer"

    monkeypatch.setitem(cli_runner._EXTERNAL_RUNNERS, "codex-tmux", _fake_runner)
    runner = CliRunner(tmp_path, "codex-tmux")

    output = runner.run("secret prompt", timeout=42)

    assert output == "tmux answer"
    assert calls == [("secret prompt", tmp_path, 42)]
    assert "secret prompt" not in runner.build_command("secret prompt")


@pytest.mark.parametrize("model", [None, "example-mini"])
def test_kimi_uses_acp_stdio_without_prompt_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str | None
) -> None:
    executable = tmp_path / "kimi"
    marker = tmp_path / "request.json"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import sys
            from pathlib import Path

            def send(message):
                print(json.dumps(message), flush=True)

            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                if method == "initialize":
                    send({{
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {{
                            "protocolVersion": request["params"]["protocolVersion"],
                            "agentCapabilities": {{}},
                        }},
                    }})
                elif method == "session/new":
                    send({{
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {{"sessionId": "test-session"}},
                    }})
                elif method == "session/prompt":
                    prompt = request["params"]["prompt"][0]["text"]
                    Path(os.environ["KIMI_TEST_MARKER"]).write_text(json.dumps({{
                        "argv": sys.argv[1:],
                        "prompt": prompt,
                    }}))
                    send({{
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {{
                            "sessionId": "test-session",
                            "update": {{
                                "sessionUpdate": "agent_message_chunk",
                                "content": {{
                                    "type": "text",
                                    "text": "{{\\\"ok\\\": true}}",
                                }},
                            }},
                        }},
                    }})
                    send({{
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {{"stopReason": "end_turn"}},
                    }})
            """
        )
    )
    executable.chmod(0o755)
    env_path = f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"
    runner = CliRunner(tmp_path, "kimi")
    prompt = "secret prompt that must stay off argv"
    monkeypatch.setattr(cli_runner, "resolve_light_model", lambda *a, **k: model)

    output = runner.run(
        prompt,
        timeout=5,
        extra_env={"PATH": env_path, "KIMI_TEST_MARKER": str(marker)},
        light=model is not None,
    )

    assert output == '{"ok": true}'
    request = json.loads(marker.read_text())
    argv = ["--model", model, "acp"] if model else ["acp"]
    assert request == {"argv": argv, "prompt": prompt}


@pytest.mark.parametrize(
    "backend",
    ["claude", "codex", "qwen", "gemini", "opencode"],
)
def test_large_prompt_uses_backend_stdin_prefix(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    runner = CliRunner(Path("."), backend)
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, stdout='{"type":"result","result":"ok"}\n'),
    )

    try:
        runner.run("x" * (128 * 1024 + 1), timeout=10)
    except CliExecutionError:
        pass

    assert calls[0]["args"] == (list(runner.spec.stdin_prefix),)
    assert calls[0]["input"] == "x" * (128 * 1024 + 1)


@pytest.mark.parametrize(
    "backend",
    ["claude", "codex", "qwen", "gemini", "opencode"],
)
def test_small_prompt_uses_backend_stdin_prefix(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    runner = CliRunner(Path("."), backend)
    calls: list[dict[str, object]] = []
    prompt = "hello"

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, stdout='{"type":"result","result":"ok"}\n'),
    )

    try:
        runner.run(prompt, timeout=10)
    except CliExecutionError:
        pass

    assert calls[0]["args"] == (list(runner.spec.stdin_prefix),)
    assert calls[0]["input"] == prompt


def test_grok_keeps_prompt_in_argv_without_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "grok")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, stdout='{"type":"text","data":"ok"}\n'),
    )

    assert runner.run("hello", timeout=10) == "ok"
    assert calls[0]["args"] == ([*runner.spec.argv_prefix, "hello"],)
    assert calls[0]["input"] is None


def test_recover_cli_text_from_qwen_stream() -> None:
    raw = "\n".join(
        [
            '{"type":"system","subtype":"init"}',
            (
                '{"type":"assistant","message":{"content":['
                '{"type":"thinking","thinking":"..."}'
                ',{"type":"text","text":"{\\"ok\\": true}"}]}}'
            ),
            '{"type":"result","result":"{\\"ok\\": true}"}',
        ]
    )
    assert recover_cli_text_from_raw_stream("qwen", raw) == '{"ok": true}'


def test_recover_cli_text_from_codex_stream() -> None:
    raw = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            (
                '{"type":"item.completed","item":'
                '{"type":"agent_message","id":"m1","text":"{\\"ok\\": true}"}}'
            ),
            '{"type":"turn.completed"}',
        ]
    )
    assert recover_cli_text_from_raw_stream("codex", raw) == '{"ok": true}'


def test_runner_decodes_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner(Path("."), "qwen")

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(
            [],
            stdout=(
                '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"{\\"ok\\": true}"}]}}\n'
                '{"type":"result","result":"{\\"ok\\": true}"}\n'
            ),
        ),
    )

    output = runner.run("hello", timeout=10)

    assert output == '{"ok": true}'


def test_runner_wraps_oserror_as_cli_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "qwen")

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.Popen", fake_popen)

    with pytest.raises(CliExecutionError, match="Argument list too long"):
        runner.run("x" * 10, timeout=10)


def test_detect_terminal_backend_message_recognizes_quota_exhaustion() -> None:
    message = (
        "Qwen OAuth quota exceeded: Your free daily quota has been reached."
    )

    assert detect_terminal_backend_message(message) == message


def test_detect_terminal_backend_message_ignores_explanatory_answer() -> None:
    message = "Rate limit means too many requests during a time window."

    assert detect_terminal_backend_message(message) is None


def test_recover_cli_error_from_codex_turn_failed_stream() -> None:
    raw = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.started"}',
            (
                '{"type":"turn.failed","error":'
                '{"message":"You\'ve hit your usage limit."}}'
            ),
        ]
    )
    assert (
        recover_cli_error_from_raw_stream("codex", raw)
        == "You've hit your usage limit."
    )


def test_recover_cli_error_from_codex_error_event_stream() -> None:
    raw = '{"type":"error","message":"Backend unavailable"}'
    assert (
        recover_cli_error_from_raw_stream("codex", raw) == "Backend unavailable"
    )


def test_recover_cli_error_returns_empty_for_unknown_backend() -> None:
    assert recover_cli_error_from_raw_stream("unknown", "anything") == ""


def test_runner_prefers_structured_error_over_stderr_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "codex")

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(
            [],
            stdout=(
                '{"type":"thread.started","thread_id":"t1"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"turn.failed","error":'
                '{"message":"You\'ve hit your usage limit."}}\n'
            ),
            stderr="Reading additional input from stdin...\n",
            returncode=1,
        ),
    )

    with pytest.raises(CliExecutionError) as excinfo:
        runner.run("hello", timeout=10)

    assert str(excinfo.value) == "You've hit your usage limit."
    assert excinfo.value.returncode == 1


def test_runner_falls_back_to_stderr_when_no_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "codex")

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(
            [],
            stdout="",
            stderr="codex: command not found\n",
            returncode=2,
        ),
    )

    with pytest.raises(CliExecutionError, match="codex: command not found"):
        runner.run("hello", timeout=10)


def test_runner_raises_cli_execution_error_on_terminal_backend_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "qwen")

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(
            [],
            stdout=(
                '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"Qwen OAuth quota exceeded: '
                'Your free daily quota has been reached."}]}}\n'
                '{"type":"result","result":"Qwen OAuth quota exceeded: '
                'Your free daily quota has been reached."}\n'
            ),
        ),
    )

    with pytest.raises(CliExecutionError, match="quota exceeded"):
        runner.run("hello", timeout=10)


def test_runner_reports_stream_error_when_exit_code_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "qwen")

    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(
            [],
            stdout=(
                '{"type":"result","subtype":"error_during_execution",'
                '"is_error":true,"error":{"message":'
                '"Qwen OAuth free tier was discontinued."}}\n'
            ),
        ),
    )

    with pytest.raises(CliExecutionError, match="free tier was discontinued"):
        runner.run("hello", timeout=10)


def test_recover_cli_text_from_grok_stream() -> None:
    raw = "\n".join(
        [
            '{"type":"text","data":"{\\"ok\\": "}',
            '{"type":"thought","data":"ignored"}',
            '{"type":"text","data":"true}"}',
            '{"type":"end","sessionId":"s1"}',
        ]
    )
    assert recover_cli_text_from_raw_stream("grok", raw) == '{"ok": true}'


def test_recover_cli_error_from_grok_stream() -> None:
    raw = '{"type":"error","message":"Backend unavailable"}'
    assert recover_cli_error_from_raw_stream("grok", raw) == "Backend unavailable"


def test_recover_cli_text_from_opencode_stream() -> None:
    raw = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1","part":{"type":"step-start"}}',
            '{"type":"reasoning","sessionID":"ses_1","part":{"text":"ignored"}}',
            (
                '{"type":"text","sessionID":"ses_1","part":'
                '{"type":"text","text":"context","synthetic":true}}'
            ),
            (
                '{"type":"text","sessionID":"ses_1","part":'
                '{"type":"text","text":"{\\"ok\\": true}"}}'
            ),
        ]
    )
    assert recover_cli_text_from_raw_stream("opencode", raw) == '{"ok": true}'


def test_recover_cli_error_from_opencode_stream() -> None:
    raw = (
        '{"type":"error","sessionID":"ses_1","error":'
        '{"name":"ProviderAuthError","data":{"message":"missing credentials"}}}'
    )
    assert (
        recover_cli_error_from_raw_stream("opencode", raw) == "missing credentials"
    )


def test_gemini_full_message_replaces_streamed_text() -> None:
    raw = "\n".join(
        [
            '{"type":"message","role":"assistant","delta":true,"content":"par"}',
            '{"type":"message","role":"assistant","delta":true,"content":"tial"}',
            '{"type":"message","role":"assistant","content":"final answer"}',
        ]
    )
    assert recover_cli_text_from_raw_stream("gemini", raw) == "final answer"


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="process tree assertions need /proc",
)
def test_timeout_stops_processes_spawned_by_the_cli(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    executable = tmp_path / "qwen"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path

            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            Path(os.environ["CHILD_PID_FILE"]).write_text(str(child.pid))
            time.sleep(60)
            """
        )
    )
    executable.chmod(0o755)
    env_path = f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"
    runner = CliRunner(tmp_path, "qwen")

    with pytest.raises(TimeoutError):
        runner.run(
            "hello",
            timeout=2,
            extra_env={"PATH": env_path, "CHILD_PID_FILE": str(marker)},
        )

    child_pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _process_alive(child_pid)


# --- regression tests for the G1 "cli-runner" audit fixes ---
#
# Covers: environment allowlisting for AI CLI subprocesses (cli_runner and
# todoist_projects), a bounded ``communicate()`` after a timed-out CLI,
# conservative quota-message detection, a timeout on the Todoist
# ``find-projects`` call, and the claude-tmux stall watchdog / transcript
# cleanup.


_COMPOSER_SCREEN = "\n❯ Try \"refactor <filepath>\"\n  ⏵⏵ bypass permissions on\n"


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

    monkeypatch.setattr(todoist_projects, "run_in_process_group", fake_run)

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

    monkeypatch.setattr(
        "d_brain.services.todoist_tasks.run_in_process_group", fake_run
    )

    processor._create_todoist_tasks([{"content": "Buy milk"}])

    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["TODOIST_API_KEY"] == "todoist-secret"
    assert captured["PATH"] == f"{tmp_path / '.venv' / 'bin'}:/usr/bin"
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

    monkeypatch.setattr("d_brain.services.processor.run_in_process_group", fake_run)

    processor._run_uv_script("--version")

    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["PATH"] == "/usr/bin"


# --- B) bounded communicate() after a timed-out CLI ----------------------


class _FakeStuckProcess:
    """A CLI whose grandchild keeps holding the stdout pipe open."""

    def __init__(self) -> None:
        # Not a real pid/pgid on this machine, so `subprocess_group.stop_process_group`
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
    (the one call below comes from `subprocess_group.stop_process_group`'s own OSError
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
    # one from `subprocess_group.stop_process_group`'s OSError branch,
    # one from the fallback.
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

    monkeypatch.setattr(todoist_projects, "run_in_process_group", fake_run)

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

    monkeypatch.setattr(todoist_projects, "run_in_process_group", fake_run)

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


def _wait_pid_dead(pid: int, seconds: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state in {"Z", "X"}:
            return True
        time.sleep(0.05)
    return False


def test_run_uv_script_timeout_leaves_no_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pid"
    fake_uv = tmp_path / "fake-uv"
    fake_uv.write_text(
        f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nwait\n", encoding="utf-8"
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("UV_BIN", str(fake_uv))
    monkeypatch.setattr(CliProcessor, "_UV_SCRIPT_TIMEOUT_SECONDS", 1)
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    processor = CliProcessor(vault_path)

    assert processor._run_uv_script("x") is False

    assert _wait_pid_dead(int(pid_file.read_text()))
