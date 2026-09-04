from __future__ import annotations

import json
from pathlib import Path

import pytest

from d_brain.services import codex_tmux
from d_brain.services.codex_tmux import run_codex_tmux


class _FakeTmux:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple[list[str], str | None]] = []
        self.prompt = ""
        self.capture_count = 0

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        args = list(argv)
        input_text = kwargs.get("input")
        self.calls.append((args, input_text))
        command = args[3]
        stdout = ""
        if command == "capture-pane":
            self.capture_count += 1
            if self.capture_count == 1:
                stdout = "model:       loading\ndirectory:   /work\n› Ask Codex\n"
            elif self.capture_count == 2:
                stdout = (
                    "Do you trust the contents of this directory?\n"
                    "› 1. Yes, continue\nPress enter to continue\n"
                )
            else:
                stdout = "model: gpt-5\ndirectory: /work\n› Ask Codex to do anything\n"
        elif command == "list-panes":
            stdout = "0 0\n"
        elif command == "load-buffer":
            self.prompt = str(input_text or "")
        elif command == "send-keys" and args[-1] == "Enter" and self.prompt:
            path = (
                self.home
                / ".codex"
                / "sessions"
                / "2026"
                / "09"
                / "04"
                / (
                    "rollout-2026-09-04T09-00-00-"
                    "11111111-1111-4111-8111-111111111111.jsonl"
                )
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {"type": "session_meta", "payload": {"cwd": "/work"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": self.prompt}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "output": "tool noise",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Готово"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "Готово",
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": stdout, "stderr": ""},
        )()


def test_run_codex_tmux_uses_buffer_reads_rollout_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(tmp_path)
    monkeypatch.setattr(codex_tmux.subprocess, "run", fake)
    monkeypatch.setattr(codex_tmux.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(codex_tmux, "_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(codex_tmux, "_PASTE_SETTLE", 0.0)

    output = run_codex_tmux(
        "secret prompt",
        Path("/work"),
        {"HOME": str(tmp_path)},
        30,
    )

    assert output == "Готово"
    new_session = next(args for args, _ in fake.calls if args[3] == "new-session")
    assert "secret prompt" not in new_session
    assert "check_for_update_on_startup=false" in new_session
    assert fake.capture_count == 3
    assert any(input_text == "secret prompt" for _, input_text in fake.calls)
    assert list((tmp_path / ".codex" / "sessions").rglob("*.jsonl")) == []


def test_scan_rollout_ignores_tool_output(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "prompt"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call_output", "output": "secret"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "answer",
                    },
                },
            )
        ),
        encoding="utf-8",
    )

    state = codex_tmux._scan_rollout(path, " prompt\n")

    assert state.prompt_seen is True
    assert state.turn_finished is True
    assert state.assistant_text == "answer"


def test_wait_for_composer_does_not_submit_while_codex_is_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(Path("/tmp"))
    monkeypatch.setattr(codex_tmux.subprocess, "run", fake)
    monkeypatch.setattr(codex_tmux, "_COMPOSER_TIMEOUT", 0.0)

    with pytest.raises(TimeoutError, match="did not become ready"):
        codex_tmux._wait_for_composer("socket", {"HOME": "/tmp"}, float("inf"))

    assert not any(args[3] == "load-buffer" for args, _ in fake.calls)
