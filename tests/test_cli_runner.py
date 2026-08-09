import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from d_brain.services import cli_runner
from d_brain.services.cli_json_stream import (
    recover_cli_error_from_raw_stream,
    recover_cli_text_from_raw_stream,
)
from d_brain.services.cli_runner import (
    CliExecutionError,
    CliRunner,
    detect_terminal_backend_message,
    normalize_ai_cli,
)


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

        def __enter__(self) -> "_FakeProcess":
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


@pytest.mark.parametrize(
    "value",
    [
        "claude",
        "claude-tmux",
        "codex",
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


def test_kimi_uses_acp_stdio_without_prompt_in_argv(tmp_path: Path) -> None:
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

    output = runner.run(
        prompt,
        timeout=5,
        extra_env={"PATH": env_path, "KIMI_TEST_MARKER": str(marker)},
    )

    assert output == '{"ok": true}'
    request = json.loads(marker.read_text())
    assert request == {"argv": ["acp"], "prompt": prompt}


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
        time.sleep(0.1)
    assert not _process_alive(child_pid)
