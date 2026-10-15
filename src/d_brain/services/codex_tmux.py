"""Run Codex inside an isolated tmux session and read its rollout."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_NAME = "dbrain"
_BUFFER_NAME = "dbrain-prompt"
_TMUX_TIMEOUT = 15.0
_POLL_INTERVAL = 1.0
_COMPOSER_TIMEOUT = 60.0
_PROMPT_ACCEPT_TIMEOUT = 30.0
_PASTE_SETTLE = 1.0
_STALL_SECONDS_ENV = "CODEX_TMUX_STALL_SECONDS"
_DEFAULT_STALL_SECONDS = 1500.0
_HEADER_LOADING_RE = re.compile(r"\b(?:model|directory):\s+loading\b")
_TRUST_DIALOG_MARKER = "do you trust the contents of this directory"


class CodexTmuxError(RuntimeError):
    """Raised when the tmux session cannot deliver a prompt or an answer."""


@dataclass
class _RolloutState:
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
        raise CodexTmuxError(f"tmux {args[0]} failed: {detail or 'unknown error'}")
    return result.stdout


def _rollout_root(env: Mapping[str, str]) -> Path:
    configured = str(env.get("CODEX_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser() / "sessions"
    home = str(env.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".codex" / "sessions"


def _rollout_paths(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return set(root.glob("*/*/*/rollout-*.jsonl"))


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "").strip()
        for item in content
        if isinstance(item, dict)
        and str(item.get("type") or "").strip().lower()
        in {"text", "input_text", "output_text"}
        and str(item.get("text") or "").strip()
    )


def _scan_rollout(path: Path, prompt: str) -> _RolloutState:
    state = _RolloutState()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return state

    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        record_type = str(record.get("type") or "").strip()
        payload_type = str(payload.get("type") or "").strip()
        if record_type == "response_item" and payload_type == "message":
            role = str(payload.get("role") or "").strip()
            text = _text_from_content(payload.get("content"))
            if role == "user" and text == prompt.strip():
                state.prompt_seen = True
            elif role == "assistant" and text:
                state.assistant_text = text
            continue
        if record_type != "event_msg":
            continue
        if payload_type == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if text:
                state.assistant_text = text
            state.turn_finished = True
    return state


def _find_rollout(
    root: Path,
    existing: set[Path],
    workdir: Path,
    prompt: str,
) -> Path | None:
    target = os.path.realpath(workdir)
    candidates = sorted(
        _rollout_paths(root) - existing,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            first = json.loads(raw.splitlines()[0])
        except (OSError, ValueError, IndexError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        cwd = str(payload.get("cwd") or "") if isinstance(payload, dict) else ""
        if os.path.realpath(cwd) != target:
            continue
        if _scan_rollout(path, prompt).prompt_seen:
            return path
    return None


def _fail_if_pane_died(socket: str, env: Mapping[str, str]) -> None:
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
        raise CodexTmuxError("codex tmux session disappeared before answering")
    dead, _, status = output.splitlines()[0].partition(" ")
    if dead.strip() == "1":
        raise CodexTmuxError(
            f"codex exited inside tmux with status {status.strip() or 'unknown'}"
        )


def _wait_for_composer(socket: str, env: Mapping[str, str], deadline: float) -> None:
    composer_deadline = min(deadline, time.monotonic() + _COMPOSER_TIMEOUT)
    while time.monotonic() < composer_deadline:
        _fail_if_pane_died(socket, env)
        screen = _tmux(socket, "capture-pane", "-p", "-t", _SESSION_NAME, env=env)
        lower = screen.lower()
        if "update available" in lower and "press enter to continue" in lower:
            raise CodexTmuxError("codex update prompt blocked the tmux composer")
        if _TRUST_DIALOG_MARKER in lower and "press enter to continue" in lower:
            _tmux(socket, "send-keys", "-t", _SESSION_NAME, "Enter", env=env)
            time.sleep(_POLL_INTERVAL)
            continue
        loading = _HEADER_LOADING_RE.search(lower) is not None
        ready = ("›" in screen or "❯" in screen) and not loading
        if ready and "starting mcp servers" not in lower:
            return
        time.sleep(_POLL_INTERVAL)
    raise TimeoutError("codex tmux composer did not become ready")


def _paste_prompt(socket: str, prompt: str, env: Mapping[str, str]) -> None:
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


def _wait_for_rollout(
    socket: str,
    root: Path,
    existing: set[Path],
    workdir: Path,
    prompt: str,
    env: Mapping[str, str],
    deadline: float,
) -> Path | None:
    accept_deadline = min(deadline, time.monotonic() + _PROMPT_ACCEPT_TIMEOUT)
    while time.monotonic() < accept_deadline:
        path = _find_rollout(root, existing, workdir, prompt)
        if path is not None:
            return path
        _fail_if_pane_died(socket, env)
        time.sleep(_POLL_INTERVAL)
    return None


def _send_prompt(
    socket: str,
    prompt: str,
    root: Path,
    existing: set[Path],
    workdir: Path,
    env: Mapping[str, str],
    deadline: float,
) -> Path:
    _wait_for_composer(socket, env, deadline)
    for attempt in range(2):
        if attempt:
            _tmux(socket, "send-keys", "-t", _SESSION_NAME, "C-c", env=env, check=False)
        _paste_prompt(socket, prompt, env)
        _tmux(socket, "send-keys", "-t", _SESSION_NAME, "Enter", env=env)
        path = _wait_for_rollout(
            socket, root, existing, workdir, prompt, env, deadline
        )
        if path is not None:
            return path
        if time.monotonic() >= deadline:
            raise TimeoutError("codex tmux session timed out while sending the prompt")
    raise CodexTmuxError("codex did not accept the prompt in the tmux session")


def _stall_seconds(env: Mapping[str, str]) -> float:
    raw = str(env.get(_STALL_SECONDS_ENV) or "").strip()
    if not raw:
        return _DEFAULT_STALL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_STALL_SECONDS
    return value if value > 0 else _DEFAULT_STALL_SECONDS


def _wait_for_answer(
    socket: str,
    path: Path,
    prompt: str,
    env: Mapping[str, str],
    deadline: float,
) -> str:
    stall_seconds = _stall_seconds(env)
    fingerprint: tuple[int, float] | None = None
    last_progress = time.monotonic()
    while True:
        state = _scan_rollout(path, prompt)
        if state.turn_finished:
            if not state.assistant_text:
                raise CodexTmuxError("codex finished the turn without any text")
            return state.assistant_text
        _fail_if_pane_died(socket, env)
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime)
        except OSError:
            current = None
        now = time.monotonic()
        if current != fingerprint:
            fingerprint = current
            last_progress = now
        elif now - last_progress >= stall_seconds:
            raise TimeoutError(
                f"codex tmux session made no progress for {stall_seconds:.0f}s"
            )
        if now >= deadline:
            raise TimeoutError("codex tmux session timed out while answering")
        time.sleep(_POLL_INTERVAL)


def _cleanup_rollout(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove codex tmux rollout %s: %s", path, exc)


def run_codex_tmux(
    prompt: str,
    workdir: Path,
    env: Mapping[str, str],
    timeout: int,
) -> str:
    """Run one isolated Codex TUI session and return final assistant text."""

    if shutil.which("tmux") is None:
        raise CodexTmuxError("tmux is required for the codex-tmux backend")

    socket = f"dbrain-{uuid.uuid4().hex[:12]}"
    tmux_env = {**env, "TERM": "xterm-256color"}
    root = _rollout_root(env)
    existing = _rollout_paths(root)
    deadline = time.monotonic() + timeout
    rollout: Path | None = None

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
            "codex",
            "--no-alt-screen",
            "-c",
            "check_for_update_on_startup=false",
            "--dangerously-bypass-approvals-and-sandbox",
            env=tmux_env,
        )
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
        rollout = _send_prompt(
            socket, prompt, root, existing, workdir, tmux_env, deadline
        )
        answer = _wait_for_answer(socket, rollout, prompt, tmux_env, deadline)
    finally:
        _tmux(socket, "kill-server", env=tmux_env, check=False)
    if rollout is not None:
        _cleanup_rollout(rollout)
    return answer


__all__ = ["CodexTmuxError", "run_codex_tmux"]
