import json
import os
import re
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


def test_model_error_in_transcript_is_propagated(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": "unknown model"}]},
    }))
    with pytest.raises(ClaudeTmuxError, match="unknown model"):
        claude_tmux._scan_transcript(path)


@pytest.mark.parametrize("model", [None, "haiku"])
def test_run_claude_tmux_returns_text_from_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
    model: str | None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    output = run_claude_tmux(
        "line one\nline two",
        tmp_path / "work",
        {"HOME": str(home)},
        30,
        model=model,
    )

    assert output == "answer text"
    assert fake.pasted_prompt == "line one\nline two"
    command = _args_of(fake, "new-session")[0]
    assert ("--model" in command) == (model is not None)
    if model:
        assert command[command.index("--model") + 1] == model


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


def test_sanitize_tmux_prompt_normalizes_newlines_and_strips_controls() -> None:
    raw = "a\r\nb\rc\x1b[Ad\x03e\tf\x7f\x85g Привет 😀"

    assert claude_tmux.sanitize_tmux_prompt(raw) == "a\nb\nc[Ade\tfg Привет 😀"


def test_run_claude_tmux_pastes_sanitized_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    run_claude_tmux("x\r\ny\x1bz\x03", tmp_path / "work", {"HOME": str(home)}, 30)

    assert fake.pasted_prompt == "x\nyz"
    paste = _args_of(fake, "paste-buffer")[0]
    assert "-p" not in paste
    assert "-r" in paste


@pytest.fixture(autouse=True)
def _reset_session_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_tmux, "_stopping", False)
    monkeypatch.setattr(claude_tmux, "_ACTIVE", {})


def test_session_socket_name_carries_owner_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    assert re.fullmatch(
        rf"dbrain-{os.getpid()}-[0-9a-f]{{12}}", claude_tmux.new_socket_name()
    )
    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)

    run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert re.fullmatch(rf"dbrain-{os.getpid()}-[0-9a-f]{{12}}", fake.commands[0][2])


@pytest.mark.parametrize("dead_status", [None, "127"])
def test_run_claude_tmux_registers_and_unregisters_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
    dead_status: str | None,
) -> None:
    home = tmp_path / "home"
    fake = _FakeTmux(home, dead_status=dead_status)
    seen: list[set[str]] = []

    def spy(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if cmd[3] == "new-session":
            seen.append(set(claude_tmux._ACTIVE))
        return fake(cmd, **kwargs)

    monkeypatch.setattr(claude_tmux.subprocess, "run", spy)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")

    if dead_status:
        with pytest.raises(ClaudeTmuxError):
            run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)
    else:
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert seen == [{fake.commands[0][2]}]
    assert claude_tmux._ACTIVE == {}
    assert _args_of(fake, "kill-server")


def test_shutdown_tmux_sessions_kills_active_servers_and_blocks_new_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    calls: list[list[str]] = []

    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        if cmd[2] == "dbrain-1-aaaaaaaaaaaa":
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(claude_tmux.subprocess, "run", run)
    claude_tmux.register_session("dbrain-1-aaaaaaaaaaaa", {})
    claude_tmux.register_session("dbrain-2-bbbbbbbbbbbb", {})

    claude_tmux.shutdown_tmux_sessions()

    assert {c[2] for c in calls if c[3] == "kill-server"} == {
        "dbrain-1-aaaaaaaaaaaa",
        "dbrain-2-bbbbbbbbbbbb",
    }
    with pytest.raises(ClaudeTmuxError):
        claude_tmux.register_session("dbrain-3-cccccccccccc", {})

    home = tmp_path / "home"
    fake = _FakeTmux(home)
    _install(monkeypatch, fake)
    with pytest.raises(ClaudeTmuxError):
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)
    assert not _args_of(fake, "new-session")


def _make_sockets(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    sock_dir = tmp_path / f"tmux-{os.getuid()}"
    sock_dir.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    names = {
        "dead": f"dbrain-{dead.pid}-aaaaaaaaaaaa",
        "alive": f"dbrain-{os.getpid()}-bbbbbbbbbbbb",
        "other": "other-123",
        "old": "dbrain-cccccccccccc",
        "fresh": "dbrain-dddddddddddd",
    }
    paths = {key: sock_dir / name for key, name in names.items()}
    for path in paths.values():
        path.touch()
    old = time.time() - 7 * 3600
    os.utime(paths["old"], (old, old))
    return {"TMUX_TMPDIR": str(tmp_path)}, paths


def test_cleanup_orphan_sessions_kills_only_dead_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, paths = _make_sockets(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        claude_tmux.subprocess,
        "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert claude_tmux.cleanup_orphan_sessions(env) == 2

    assert sorted(c[2] for c in calls if c[3] == "kill-server") == sorted(
        [paths["dead"].name, paths["old"].name]
    )
    assert not paths["dead"].exists()
    assert not paths["old"].exists()
    for key in ("alive", "other", "fresh"):
        assert paths[key].exists()


def test_cleanup_orphan_sessions_survives_tmux_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, paths = _make_sockets(tmp_path)

    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(claude_tmux.subprocess, "run", run)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert claude_tmux.cleanup_orphan_sessions(env) == 2
    assert not paths["dead"].exists()
    assert not paths["old"].exists()


def test_cleanup_orphan_sessions_without_socket_dir_or_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        claude_tmux.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd))
    )
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: "/usr/bin/tmux")
    assert claude_tmux.cleanup_orphan_sessions({"TMUX_TMPDIR": str(tmp_path)}) == 0

    env, _ = _make_sockets(tmp_path)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: None)
    assert claude_tmux.cleanup_orphan_sessions(env) == 0
    assert calls == []


def test_shutdown_during_new_session_kills_new_server_and_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_polling: None,
) -> None:
    monkeypatch.setattr(claude_tmux, "_stopping", False)
    monkeypatch.setattr(claude_tmux, "_ACTIVE", {})
    home = tmp_path / "home"
    fake = _FakeTmux(home)

    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if cmd[3] == "new-session":
            # Shutdown ran after register_session but before the server existed.
            claude_tmux.shutdown_tmux_sessions()
        return fake(cmd, **kwargs)

    monkeypatch.setattr(claude_tmux.subprocess, "run", run)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(ClaudeTmuxError, match="stopping"):
        run_claude_tmux("prompt", tmp_path / "work", {"HOME": str(home)}, 30)

    assert not _args_of(fake, "load-buffer")
    assert _args_of(fake, "kill-server")[-1] == ["kill-server"]
    assert claude_tmux._ACTIVE == {}


def test_fail_if_pane_died_stops_polling_once_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_tmux, "_stopping", True)
    monkeypatch.setattr(
        claude_tmux.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "0 \n", ""),
    )

    with pytest.raises(ClaudeTmuxError, match="stopping"):
        claude_tmux._fail_if_pane_died("dbrain-1-aaaaaaaaaaaa", {})


def test_cleanup_default_env_has_no_tmux_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sessions run with build_subprocess_env (no TMUX_TMPDIR), so the sweep must
    # look where tmux put their sockets, not where the bot's own env points.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "elsewhere"))
    seen: list[dict[str, str]] = []

    def socket_dir(env):  # type: ignore[no-untyped-def]
        seen.append(dict(env))
        return tmp_path / "missing"

    monkeypatch.setattr(claude_tmux, "_socket_dir", socket_dir)
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: "/usr/bin/tmux")

    assert claude_tmux.cleanup_orphan_sessions() == 0
    assert seen and "TMUX_TMPDIR" not in seen[0]


def test_cleanup_ignores_huge_pid_in_socket_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock_dir = tmp_path / f"tmux-{os.getuid()}"
    sock_dir.mkdir()
    (sock_dir / ("dbrain-" + "9" * 30 + "-aaaaaaaaaaaa")).touch()
    monkeypatch.setattr(claude_tmux.shutil, "which", lambda name: "/usr/bin/tmux")

    assert claude_tmux.cleanup_orphan_sessions({"TMUX_TMPDIR": str(tmp_path)}) == 0


def test_pid_alive_treats_overflow_as_dead() -> None:
    assert claude_tmux._pid_alive(10**30) is False
