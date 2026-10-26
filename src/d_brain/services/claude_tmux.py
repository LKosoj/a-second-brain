"""Run Claude Code inside a detached tmux session.

Interactive fallback for the day headless `claude -p` disappears: the prompt is
pasted into a real TUI session and the answer is read back from the JSONL
transcript claude keeps for the session id we pass in. Reading the transcript
instead of the pane keeps the result free of ANSI escapes and TUI chrome.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d_brain.services.cli_models import model_unavailable

logger = logging.getLogger(__name__)

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
# The caller's overall `timeout` can be set generously for a legitimate long
# turn, so it alone does not catch a session that stopped making any progress
# well before that ceiling. This idle bound does, and is configurable because
# "no progress" can mean different things for different workloads. The
# transcript is only appended to once the assistant's block finishes, so the
# default here must exceed the longest turn that is expected to run with no
# tool call in between -- e.g. plaud's own call timeout
# (PLAUD_TASK_TIMEOUT = 1200s in services/plaud.py) -- or a legitimate long,
# silent turn would be killed before that deadline even fires.
_STALL_SECONDS_ENV = "CLAUDE_TMUX_STALL_SECONDS"
_DEFAULT_STALL_SECONDS = 1500.0


class ClaudeTmuxError(RuntimeError):
    """Raised when the tmux session cannot deliver a prompt or an answer."""


_SOCKET_RE = re.compile(r"^dbrain-(?:(\d{1,10})-)?[0-9a-f]{12}$")
# Sockets from older versions carry no owner pid. 6 hours is longer than the
# longest run (process.service has TimeoutStartSec=4h), so a younger one may
# still belong to a live nightly run.
_LEGACY_SOCKET_MAX_AGE = 6 * 3600.0

# Sockets of the sessions started by this process, so the bot can close them on
# stop instead of waiting for the CLI timeout.
_ACTIVE: dict[str, Mapping[str, str]] = {}
_ACTIVE_LOCK = threading.Lock()
_stopping = False


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
            if record.get("isApiErrorMessage") and model_unavailable(text):
                raise ClaudeTmuxError(text)
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

    raise_if_stopping()
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
        screen = _tmux(socket, "capture-pane", "-p", "-t", _SESSION_NAME, env=env)
        for line in screen.splitlines():
            if model_unavailable(line):
                raise ClaudeTmuxError(line.strip())
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


# Everything except \n and \t: C0 controls, DEL and C1 controls.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_tmux_prompt(text: str) -> str:
    """Make text safe to paste into a TUI composer.

    tmux hands the buffer to the application byte for byte, so a stray \r
    submits the prompt, \x1b opens an escape sequence and \x03 is Ctrl-C.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", text)


def _paste_prompt(socket: str, prompt: str, env: Mapping[str, str]) -> None:
    # load-buffer keeps the prompt out of argv (128 KB per-argument limit) and
    # `-r` stops tmux from turning its newlines into Enter presses.
    # `-p` is not used: bracketed paste support of the claude/codex TUIs was
    # not verified; control characters are stripped instead.
    _tmux(
        socket,
        "load-buffer",
        "-b",
        _BUFFER_NAME,
        "-",
        env=env,
        input_text=sanitize_tmux_prompt(prompt),
    )
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


def _stall_seconds(env: Mapping[str, str]) -> float:
    """Idle threshold before a silently stuck turn is treated as failed."""

    raw = str(env.get(_STALL_SECONDS_ENV) or "").strip()
    if not raw:
        return _DEFAULT_STALL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_STALL_SECONDS
    return value if value > 0 else _DEFAULT_STALL_SECONDS


def _transcript_fingerprint(root: Path, session_id: str) -> tuple[int, float] | None:
    """Cheap (size, mtime) marker used to detect a turn making no progress."""

    path = _find_transcript(root, session_id)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime)


def _wait_for_answer(
    socket: str,
    root: Path,
    session_id: str,
    env: Mapping[str, str],
    deadline: float,
) -> str:
    stall_seconds = _stall_seconds(env)
    fingerprint = _transcript_fingerprint(root, session_id)
    last_progress = time.monotonic()
    while True:
        state = _read_state(root, session_id)
        if state.turn_finished:
            if not state.assistant_text:
                raise ClaudeTmuxError("claude finished the turn without any text")
            return state.assistant_text
        _fail_if_pane_died(socket, env)
        current = _transcript_fingerprint(root, session_id)
        now = time.monotonic()
        if current != fingerprint:
            fingerprint = current
            last_progress = now
        elif now - last_progress >= stall_seconds:
            raise TimeoutError(
                f"claude tmux session made no progress for {stall_seconds:.0f}s"
            )
        if now >= deadline:
            raise TimeoutError("claude tmux session timed out while answering")
        time.sleep(_POLL_INTERVAL)


def _cleanup_transcript(root: Path, session_id: str) -> None:
    """Remove the session transcript after a successful run.

    Transcripts hold the full prompt (MEMORY, daily notes, business/CRM
    context...) and claude never rotates them itself, so keeping them around
    after a successful turn is unnecessary retention of private vault
    content. A failed turn keeps its transcript so it stays inspectable.
    """

    path = _find_transcript(root, session_id)
    if path is None:
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove claude tmux transcript %s: %s", path, exc)


def new_socket_name() -> str:
    """Name a private tmux socket after the process that owns the session.

    The pid lets a later start tell an abandoned session (owner dead) from a
    live one, e.g. the nightly run, which shares the same socket directory.
    """

    return f"dbrain-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def register_session(socket: str, env: Mapping[str, str]) -> None:
    with _ACTIVE_LOCK:
        if _stopping:
            raise ClaudeTmuxError("bot is stopping, tmux session not started")
        _ACTIVE[socket] = env


def raise_if_stopping() -> None:
    """Abort a session thread once the bot has begun shutting down.

    register_session only guards the start: a thread that passed it before
    shutdown_tmux_sessions ran has no server to kill yet, and would otherwise
    create one and wait for the CLI timeout.
    """

    with _ACTIVE_LOCK:
        stopping = _stopping
    if stopping:
        raise ClaudeTmuxError("bot is stopping, tmux session aborted")


def unregister_session(socket: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(socket, None)


def shutdown_tmux_sessions() -> None:
    """Kill every tmux server this process started and refuse new ones.

    The worker threads then see "session disappeared" on their next poll and
    exit within seconds instead of waiting for the CLI timeout.
    """

    global _stopping
    with _ACTIVE_LOCK:
        _stopping = True
        active = dict(_ACTIVE)
    for socket, env in active.items():
        try:
            _tmux(socket, "kill-server", env=env, check=False)
        except Exception as exc:
            logger.warning("Failed to stop tmux session %s: %s", socket, exc)


def _socket_dir(env: Mapping[str, str]) -> Path:
    # Same place tmux builds its socket path from.
    return Path(env.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def cleanup_orphan_sessions(env: Mapping[str, str] | None = None) -> int:
    """Kill tmux servers left by a bot that died without closing them.

    Needed at start because SIGKILL, a crash, or launchd (no cgroup kill) leave
    the detached tmux server running next to the new bot. Sessions whose owner
    pid is alive (the nightly run, this process) are left alone. Returns the
    number of servers removed.
    """

    if env is None:
        # Sessions are created with build_subprocess_env, which drops
        # TMUX_TMPDIR, so their sockets live in the default /tmp/tmux-<uid>.
        env = {k: v for k, v in os.environ.items() if k != "TMUX_TMPDIR"}
    if shutil.which("tmux") is None:
        return 0
    try:
        entries = list(_socket_dir(env).iterdir())
    except OSError:
        return 0
    killed = 0
    for path in entries:
        match = _SOCKET_RE.match(path.name)
        if match is None:
            continue
        if match.group(1) is not None:
            if _pid_alive(int(match.group(1))):
                continue
        else:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < _LEGACY_SOCKET_MAX_AGE:
                continue
        try:
            _tmux(path.name, "kill-server", env=env, check=False)
        except Exception as exc:
            logger.warning("Failed to kill tmux session %s: %s", path.name, exc)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        logger.warning("Killed abandoned tmux session %s", path.name)
        killed += 1
    return killed


def run_claude_tmux(
    prompt: str,
    workdir: Path,
    env: Mapping[str, str],
    timeout: int,
    *,
    model: str | None = None,
) -> str:
    """Run one isolated claude TUI session and return final assistant text."""

    if shutil.which("tmux") is None:
        raise ClaudeTmuxError("tmux is required for the claude-tmux backend")

    session_id = str(uuid.uuid4())
    socket = new_socket_name()
    # A private socket keeps this session away from the user's own tmux server
    # and lets the tmux server inherit the environment prepared for the CLI.
    # TERM=dumb (fine for headless) would break the TUI.
    tmux_env = {**env, "TERM": "xterm-256color"}
    root = _transcript_root(env)
    deadline = time.monotonic() + timeout

    try:
        register_session(socket, tmux_env)
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
            *(["--model", model] if model else []),
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
            env=tmux_env,
        )
        # Shutdown may have run while the server did not exist yet; finally
        # below kills the one just created.
        raise_if_stopping()
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
        answer = _wait_for_answer(socket, root, session_id, tmux_env, deadline)
    finally:
        _tmux(socket, "kill-server", env=tmux_env, check=False)
        unregister_session(socket)
    _cleanup_transcript(root, session_id)
    return answer


__all__ = [
    "ClaudeTmuxError",
    "cleanup_orphan_sessions",
    "new_socket_name",
    "raise_if_stopping",
    "register_session",
    "run_claude_tmux",
    "sanitize_tmux_prompt",
    "shutdown_tmux_sessions",
    "unregister_session",
]
