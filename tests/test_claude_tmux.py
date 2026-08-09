import json
import subprocess
import time
from pathlib import Path

import pytest

from d_brain.services import claude_tmux
from d_brain.services.claude_tmux import ClaudeTmuxError, run_claude_tmux

_COMPOSER_SCREEN = "\n❯ Try \"refactor <filepath>\"\n  ⏵⏵ bypass permissions on\n"
_TRUST_SCREEN = (
    "Quick safety check: Is this a project you created or one you trust?\n"
    "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
)


class _FakeTmux:
    """Stand-in for the tmux binary that answers like a real session."""

    def __init__(
        self,
        home: Path,
        *,
        screens: list[str] | None = None,
        dead_status: str | None = None,
        answer: str | None = "answer text",
    ) -> None:
        self.home = home
        self.screens = screens or [_COMPOSER_SCREEN]
        self.dead_status = dead_status
        self.answer = answer
        self.commands: list[list[str]] = []
        self.pasted_prompt = ""
        self.session_id = ""

    def _transcript(self) -> Path:
        path = self.home / ".claude" / "projects" / "-fake-work"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{self.session_id}.jsonl"

    def _write_turn(self) -> None:
        records: list[dict[str, object]] = [
            {"type": "user", "message": {"content": self.pasted_prompt}}
        ]
        if self.answer is not None:
            records.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": self.answer}],
                        "stop_reason": "end_turn",
                    },
                }
            )
        records.append({"type": "system", "subtype": "turn_duration"})
        self._transcript().write_text(
            "\n".join(json.dumps(record) for record in records),
            encoding="utf-8",
        )

    def __call__(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append(list(cmd))
        args = list(cmd[3:])
        stdout = ""
        if args[0] == "new-session":
            self.session_id = args[args.index("--session-id") + 1]
        elif args[0] == "capture-pane":
            stdout = self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)
        elif args[0] == "list-panes":
            stdout = f"1 {self.dead_status}\n" if self.dead_status else "0 \n"
        elif args[0] == "load-buffer":
            self.pasted_prompt = str(kwargs.get("input") or "")
        elif args[0] == "send-keys" and args[-1] == "Enter" and self.pasted_prompt:
            self._write_turn()
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


@pytest.fixture
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_tmux, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(claude_tmux, "_PASTE_SETTLE", 0.0)
    monkeypatch.setattr(claude_tmux, "_COMPOSER_TIMEOUT", 1.0)
    monkeypatch.setattr(claude_tmux, "_PROMPT_ACCEPT_TIMEOUT", 1.0)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeTmux) -> None:
    monkeypatch.setattr(claude_tmux.subprocess, "run", fake)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")


def _args_of(fake: _FakeTmux, name: str) -> list[list[str]]:
    return [command[3:] for command in fake.commands if command[3] == name]


def test_run_claude_tmux_returns_text_from_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    output = run_claude_tmux(
        "line one\nline two",
        tmp_path / "work",
        {"HOME": str(home)},
        30,
    )

    assert output == "answer text"
    assert fake.pasted_prompt == "line one\nline two"


def test_run_claude_tmux_pastes_prompt_without_argv_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    new_session = _args_of(fake, "new-session")[0]
    assert "prompt" not in new_session
    assert new_session[-4:] == [
        "claude",
        "--dangerously-skip-permissions",
        "--session-id",
        fake.session_id,
    ]
    # `-r` keeps newlines as newlines instead of Enter presses.
    assert "-r" in _args_of(fake, "paste-buffer")[0]
    assert _args_of(fake, "kill-server")


def test_run_claude_tmux_answers_the_trust_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home, screens=[_TRUST_SCREEN, _COMPOSER_SCREEN])
    _install(monkeypatch, fake)

    output = run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert output == "answer text"
    # One Enter for the dialog, one for the prompt.
    assert len(_args_of(fake, "send-keys")) == 2


def test_run_claude_tmux_reports_a_dead_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home, dead_status="127")
    _install(monkeypatch, fake)

    with pytest.raises(ClaudeTmuxError) as excinfo:
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert "status 127" in str(excinfo.value)
    assert _args_of(fake, "kill-server")


def test_run_claude_tmux_requires_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: None)

    with pytest.raises(ClaudeTmuxError) as excinfo:
        run_claude_tmux("prompt", tmp_path, {"HOME": str(tmp_path)}, 30)

    assert "tmux is required" in str(excinfo.value)


def test_run_claude_tmux_rejects_a_turn_without_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home, answer=None)
    _install(monkeypatch, fake)

    with pytest.raises(ClaudeTmuxError) as excinfo:
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert "without any text" in str(excinfo.value)


def test_waiting_for_an_answer_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    with pytest.raises(TimeoutError):
        claude_tmux._wait_for_answer(
            "socket",
            home / ".claude" / "projects",
            "missing-session",
            {"HOME": str(home)},
            time.monotonic() - 1,
        )


def test_scan_transcript_ignores_subagent_turns(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "user", "message": {"content": "prompt"}},
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {
                        "content": [{"type": "text", "text": "subagent answer"}],
                        "stop_reason": "end_turn",
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "working on it"},
                            {"type": "tool_use", "name": "Read"},
                        ],
                        "stop_reason": "tool_use",
                    },
                },
            )
        ),
        encoding="utf-8",
    )

    state = claude_tmux._scan_transcript(transcript)

    assert state.prompt_seen is True
    assert state.turn_finished is False
    assert state.assistant_text == "working on it"


def test_scan_transcript_takes_the_last_assistant_text(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "first"}],
                        "stop_reason": "tool_use",
                    },
                },
                {"type": "user", "message": {"content": "tool result"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "stop_reason": "end_turn",
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            )
        ),
        encoding="utf-8",
    )

    state = claude_tmux._scan_transcript(transcript)

    assert state.turn_finished is True
    assert state.assistant_text == '{"ok": true}'
