import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "value",
    ["claude", "codex", "qwen", "gemini", "kimi", "CLAUDE", " QWEN "],
)
def test_normalize_ai_cli_accepts_supported_values(value: str) -> None:
    normalized = normalize_ai_cli(value)
    assert normalized in {"claude", "codex", "qwen", "gemini", "kimi"}


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
                "--print",
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "-p",
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


@pytest.mark.parametrize("backend", ["claude", "codex", "qwen", "gemini"])
def test_large_prompt_uses_backend_stdin_prefix(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    runner = CliRunner(Path("."), backend)
    calls: list[dict[str, object]] = []

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"type":"result","result":"ok"}\n',
            stderr="",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    try:
        runner.run("x" * (128 * 1024 + 1), timeout=10)
    except CliExecutionError:
        pass

    assert calls[0]["args"] == (list(runner.spec.stdin_prefix),)
    assert calls[0]["kwargs"]["input"] == "x" * (128 * 1024 + 1)


@pytest.mark.parametrize("backend", ["claude", "codex", "qwen", "gemini"])
def test_small_prompt_uses_backend_stdin_prefix(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    runner = CliRunner(Path("."), backend)
    calls: list[dict[str, object]] = []
    prompt = "hello"

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"type":"result","result":"ok"}\n',
            stderr="",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    try:
        runner.run(prompt, timeout=10)
    except CliExecutionError:
        pass

    assert calls[0]["args"] == (list(runner.spec.stdin_prefix),)
    assert calls[0]["kwargs"]["input"] == prompt


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

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"{\\"ok\\": true}"}]}}\n'
                '{"type":"result","result":"{\\"ok\\": true}"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    output = runner.run("hello", timeout=10)

    assert output == '{"ok": true}'


def test_runner_wraps_oserror_as_cli_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "qwen")

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

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

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=(
                '{"type":"thread.started","thread_id":"t1"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"turn.failed","error":'
                '{"message":"You\'ve hit your usage limit."}}\n'
            ),
            stderr="Reading additional input from stdin...\n",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    with pytest.raises(CliExecutionError) as excinfo:
        runner.run("hello", timeout=10)

    assert str(excinfo.value) == "You've hit your usage limit."
    assert excinfo.value.returncode == 1


def test_runner_falls_back_to_stderr_when_no_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "codex")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="",
            stderr="codex: command not found\n",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    with pytest.raises(CliExecutionError, match="codex: command not found"):
        runner.run("hello", timeout=10)


def test_runner_raises_cli_execution_error_on_terminal_backend_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner(Path("."), "qwen")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"Qwen OAuth quota exceeded: '
                'Your free daily quota has been reached."}]}}\n'
                '{"type":"result","result":"Qwen OAuth quota exceeded: '
                'Your free daily quota has been reached."}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("d_brain.services.cli_runner.subprocess.run", fake_run)

    with pytest.raises(CliExecutionError, match="quota exceeded"):
        runner.run("hello", timeout=10)
