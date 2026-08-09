"""Run Claude Code inside a detached tmux session.

Interactive fallback for the day headless `claude -p` disappears: the prompt is
pasted into a real TUI session and the answer is read back from the JSONL
transcript claude keeps for the session id we pass in. Reading the transcript
instead of the pane keeps the result free of ANSI escapes and TUI chrome.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SESSION_NAME = "dbrain"
_BUFFER_NAME = "dbrain-prompt"
# tmux answers instantly; this only guards a wedged server.
_TMUX_TIMEOUT = 15.0
_POLL_INTERVAL = 1.0
# Time budget for the TUI to boot and draw its input composer.
_COMPOSER_TIMEOUT = 60.0
_COMPOSER_MARKER = "❯"
_TRUST_DIALOG_MARKER = "trust this folder"
# claude records the submitted prompt within a second; the rest is slack.
_PROMPT_ACCEPT_TIMEOUT = 30.0
_PASTE_SETTLE = 1.0


class ClaudeTmuxError(RuntimeError):
    """Raised when the tmux session cannot deliver a prompt or an answer."""


@dataclass
class _TranscriptState:
    """What the session transcript says about the current turn."""

    prompt_seen: bool = False
    turn_finished: bool = False
    assistant_text: str = ""


def _tmux(
    socket: str,
    *args: str,
    env: Mapping[str, str],
    input_text: str | None = None,
    check: bool = True,
) -> str:
    """Run one tmux command against our private server socket."""

    result = subprocess.run(
        ["tmux", "-L", socket, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=_TMUX_TIMEOUT,
        env=dict(env),
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ClaudeTmuxError(f"tmux {args[0]} failed: {detail or 'unknown error'}")
    return result.stdout


def _transcript_root(env: Mapping[str, str]) -> Path:
    configured = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser() / "projects"
    home = str(env.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".claude" / "projects"


def _find_transcript(root: Path, session_id: str) -> Path | None:
    """Locate the transcript claude writes for one session id."""

    # claude derives the project directory name from cwd, so globbing by
    # session id keeps this independent from that naming rule.
    for path in sorted(root.glob(f"*/{session_id}.jsonl")):
        return path
    return None


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and str(item.get("type") or "") == "text"
    ]
    return "".join(part for part in parts if part)


def _scan_transcript(path: Path) -> _TranscriptState:
    state = _TranscriptState()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return state

    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain") is True:
            # Sidechain records belong to subagents, whose turns end before
            # the main one does.
            continue

        record_type = str(record.get("type") or "")
        if record_type == "user":
            state.prompt_seen = True
            continue
        if record_type == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            text = _text_from_content(message.get("content")).strip()
            if text:
                state.assistant_text = text
            if str(message.get("stop_reason") or "") == "end_turn":
                state.turn_finished = True
            continue
        if record_type == "system" and record.get("subtype") == "turn_duration":
            state.turn_finished = True
    return state


def _read_state(root: Path, session_id: str) -> _TranscriptState:
    path = _find_transcript(root, session_id)
    if path is None:
        return _TranscriptState()
    return _scan_transcript(path)


def _fail_if_pane_died(socket: str, env: Mapping[str, str]) -> None:
    """Turn a crashed or vanished claude process into a readable error."""

    output = _tmux(
        socket,
        "list-panes",
        "-t",
        _SESSION_NAME,
        "-F",
        "#{pane_dead} #{pane_dead_status}",
        env=env,
        check=False,
    ).strip()
    if not output:
        raise ClaudeTmuxError("claude tmux session disappeared before answering")
    dead, _, status = output.splitlines()[0].partition(" ")
    if dead.strip() == "1":
        raise ClaudeTmuxError(
            f"claude exited inside tmux with status {status.strip() or 'unknown'}"
        )


def _wait_for_composer(socket: str, env: Mapping[str, str], deadline: float) -> None:
    """Wait until the TUI is ready to receive text."""

    composer_deadline = min(deadline, time.monotonic() + _COMPOSER_TIMEOUT)
    while time.monotonic() < composer_deadline:
        _fail_if_pane_died(socket, env)
        screen = _tmux(socket, "capture-pane", "-p", "-t", _SESSION_NAME, env=env)
        if _TRUST_DIALOG_MARKER in screen.lower():
            # First run in a directory: the TUI asks whether the folder is
            # trusted and preselects "yes".
            _tmux(socket, "send-keys", "-t", _SESSION_NAME, "Enter", env=env)
        elif _COMPOSER_MARKER in screen:
            return
        time.sleep(_POLL_INTERVAL)
    # The composer marker is cosmetic and may change between releases, so the
    # prompt is pasted anyway and verified through the transcript.


def _paste_prompt(socket: str, prompt: str, env: Mapping[str, str]) -> None:
    # load-buffer keeps the prompt out of argv (128 KB per-argument limit) and
    # `-r` stops tmux from turning its newlines into Enter presses.
    _tmux(socket, "load-buffer", "-b", _BUFFER_NAME, "-", env=env, input_text=prompt)
    _tmux(
        socket,
        "paste-buffer",
        "-d",
        "-r",
        "-b",
        _BUFFER_NAME,
        "-t",
        _SESSION_NAME,
        env=env,
    )
    time.sleep(_PASTE_SETTLE)


def _prompt_accepted(
    socket: str,
    root: Path,
    session_id: str,
    env: Mapping[str, str],
    deadline: float,
) -> bool:
    accept_deadline = min(deadline, time.monotonic() + _PROMPT_ACCEPT_TIMEOUT)
    while time.monotonic() < accept_deadline:
        if _read_state(root, session_id).prompt_seen:
            return True
        _fail_if_pane_died(socket, env)
        time.sleep(_POLL_INTERVAL)
    return False


def _send_prompt(
    socket: str,
    prompt: str,
    root: Path,
    session_id: str,
    env: Mapping[str, str],
    deadline: float,
) -> None:
    _wait_for_composer(socket, env, deadline)
    for attempt in range(2):
        if attempt:
            # The previous paste never reached the composer; clear whatever is
            # sitting there before retrying.
            _tmux(socket, "send-keys", "-t", _SESSION_NAME, "C-c", env=env, check=False)
        _paste_prompt(socket, prompt, env)
        _tmux(socket, "send-keys", "-t", _SESSION_NAME, "Enter", env=env)
        if _prompt_accepted(socket, root, session_id, env, deadline):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("claude tmux session timed out while sending the prompt")
    raise ClaudeTmuxError("claude did not accept the prompt in the tmux session")


def _wait_for_answer(
    socket: str,
    root: Path,
    session_id: str,
    env: Mapping[str, str],
    deadline: float,
) -> str:
    while True:
        state = _read_state(root, session_id)
        if state.turn_finished:
            if not state.assistant_text:
                raise ClaudeTmuxError("claude finished the turn without any text")
            return state.assistant_text
        _fail_if_pane_died(socket, env)
        if time.monotonic() >= deadline:
            raise TimeoutError("claude tmux session timed out while answering")
        time.sleep(_POLL_INTERVAL)


def run_claude_tmux(
    prompt: str,
    workdir: Path,
    env: Mapping[str, str],
    timeout: int,
) -> str:
    """Run one isolated claude TUI session and return final assistant text."""

    if shutil.which("tmux") is None:
        raise ClaudeTmuxError("tmux is required for the claude-tmux backend")

    session_id = str(uuid.uuid4())
    socket = f"dbrain-{uuid.uuid4().hex[:12]}"
    # A private socket keeps this session away from the user's own tmux server
    # and lets the tmux server inherit the environment prepared for the CLI.
    # TERM=dumb (fine for headless) would break the TUI.
    tmux_env = {**env, "TERM": "xterm-256color"}
    root = _transcript_root(env)
    deadline = time.monotonic() + timeout

    try:
        _tmux(
            socket,
            "new-session",
            "-d",
            "-x",
            "200",
            "-y",
            "50",
            "-s",
            _SESSION_NAME,
            "-c",
            str(workdir),
            "claude",
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
            env=tmux_env,
        )
        # Keep a crashed pane readable so its exit status reaches the caller.
        _tmux(
            socket,
            "set-option",
            "-t",
            _SESSION_NAME,
            "-w",
            "remain-on-exit",
            "on",
            env=tmux_env,
            check=False,
        )
        _send_prompt(socket, prompt, root, session_id, tmux_env, deadline)
        return _wait_for_answer(socket, root, session_id, tmux_env, deadline)
    finally:
        _tmux(socket, "kill-server", env=tmux_env, check=False)


__all__ = ["ClaudeTmuxError", "run_claude_tmux"]
