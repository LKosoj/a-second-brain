"""Small multi-CLI runner for agent backends."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from d_brain.services.claude_tmux import run_claude_tmux
from d_brain.services.cli_json_stream import (
    recover_cli_error_from_raw_stream,
    recover_cli_text_from_raw_stream,
)
from d_brain.services.kimi_acp import run_kimi_acp

AiCliName = Literal[
    "claude",
    "claude-tmux",
    "codex",
    "qwen",
    "gemini",
    "kimi",
    "grok",
    "opencode",
]

_TERMINAL_BACKEND_MARKERS = (
    "quota exceeded",
    "daily quota has been reached",
    "rate limit exceeded",
    "too many requests",
)


@dataclass(frozen=True)
class CliSpec:
    """Static command template for one supported CLI."""

    name: AiCliName
    argv_prefix: tuple[str, ...]
    stdin_prefix: tuple[str, ...] = field(default=())
    structured_output: bool = False


CLI_SPECS: dict[AiCliName, CliSpec] = {
    "claude": CliSpec(
        name="claude",
        # `--output-format stream-json` requires `--verbose` in print mode,
        # otherwise claude exits with an argument error before any request.
        argv_prefix=(
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ),
        stdin_prefix=(
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ),
        structured_output=True,
    ),
    "claude-tmux": CliSpec(
        name="claude-tmux",
        # The prompt never reaches argv here: it is pasted into a TUI session.
        argv_prefix=("claude", "--dangerously-skip-permissions"),
    ),
    "codex": CliSpec(
        name="codex",
        argv_prefix=(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
        ),
        stdin_prefix=(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
        ),
        structured_output=True,
    ),
    "qwen": CliSpec(
        name="qwen",
        argv_prefix=(
            "qwen",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--prompt",
        ),
        stdin_prefix=(
            "qwen",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--prompt",
            "",
        ),
        structured_output=True,
    ),
    "gemini": CliSpec(
        name="gemini",
        argv_prefix=(
            "gemini",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "-p",
        ),
        stdin_prefix=(
            "gemini",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "-p",
            "",
        ),
        structured_output=True,
    ),
    "kimi": CliSpec(
        name="kimi",
        argv_prefix=("kimi", "acp"),
    ),
    "grok": CliSpec(
        name="grok",
        # grok has no stdin fallback: the prompt is always an argv value.
        argv_prefix=(
            "grok",
            "--no-auto-update",
            "--always-approve",
            "--no-memory",
            "--output-format",
            "streaming-json",
            "-p",
        ),
        structured_output=True,
    ),
    "opencode": CliSpec(
        name="opencode",
        argv_prefix=("opencode", "run", "--format", "json"),
        stdin_prefix=("opencode", "run", "--format", "json"),
        structured_output=True,
    ),
}


# Backends driven by a dedicated Python runner instead of an argv/stdin call.
_EXTERNAL_RUNNERS: dict[
    AiCliName, Callable[[str, Path, Mapping[str, str], int], str]
] = {
    "kimi": run_kimi_acp,
    "claude-tmux": run_claude_tmux,
}


class CliExecutionError(RuntimeError):
    """Raised when CLI execution fails."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def normalize_ai_cli(value: str) -> AiCliName:
    """Normalize AI_CLI to a supported backend name."""

    normalized = str(value or "claude").strip().lower()
    if normalized not in CLI_SPECS:
        supported = ", ".join(sorted(CLI_SPECS))
        raise ValueError(f"Unsupported AI_CLI '{value}'. Expected one of: {supported}")
    return normalized  # type: ignore[return-value]


def _stop_process_group(proc: subprocess.Popen[str], *, grace: float = 5.0) -> None:
    """Stop a timed-out CLI together with the children it spawned."""

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        proc.kill()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def detect_terminal_backend_message(text: str) -> str | None:
    (
        "Return the original message when backend text signals a terminal "
        "execution failure."
    )

    raw = str(text or "").strip()
    normalized = " ".join(raw.lower().split())
    if not normalized:
        return None
    if "too many requests" in normalized and not (
        normalized == "too many requests"
        or normalized.startswith("error")
        or "429" in normalized
        or "quota" in normalized
    ):
        return None
    if any(marker in normalized for marker in _TERMINAL_BACKEND_MARKERS):
        return raw
    return None


class CliRunner:
    """Run one of the supported coding CLIs with a plain-text prompt."""

    def __init__(self, workdir: Path, ai_cli: str = "claude") -> None:
        self.workdir = Path(workdir)
        self.ai_cli = normalize_ai_cli(ai_cli)

    @property
    def spec(self) -> CliSpec:
        """CLI spec for the selected backend."""

        return CLI_SPECS[self.ai_cli]

    def build_command(self, prompt: str) -> list[str]:
        """Build argv for backend execution."""

        if self.ai_cli in _EXTERNAL_RUNNERS:
            return list(self.spec.argv_prefix)
        return [*self.spec.argv_prefix, prompt]

    def _decode_stdout(self, stdout: str) -> str:
        """Recover final assistant text from machine-readable output when enabled."""
        raw_text = str(stdout or "")
        if not self.spec.structured_output:
            recovered = raw_text.strip()
            terminal = detect_terminal_backend_message(recovered)
            if terminal:
                raise CliExecutionError(terminal, stdout=raw_text)
            return recovered

        recovered = recover_cli_text_from_raw_stream(self.ai_cli, raw_text)
        recovered_text = recovered.strip()
        if recovered_text:
            terminal = detect_terminal_backend_message(recovered_text)
            if terminal:
                raise CliExecutionError(terminal, stdout=raw_text)
            return recovered_text

        # A CLI can report a failed turn while still exiting with code 0
        # (quota errors from qwen/claude look like that), so surface the
        # stream error instead of the generic recovery message.
        stream_error = recover_cli_error_from_raw_stream(self.ai_cli, raw_text)
        if stream_error:
            raise CliExecutionError(stream_error, stdout=raw_text)

        raise CliExecutionError(
            "Failed to recover assistant text from structured CLI output",
            stdout=raw_text,
        )

    def run(
        self,
        prompt: str,
        *,
        timeout: int,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        """Execute prompt and return stdout text."""

        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env.setdefault("NO_COLOR", "1")
        if extra_env:
            for key, value in extra_env.items():
                if value:
                    env[key] = value

        external_runner = _EXTERNAL_RUNNERS.get(self.ai_cli)
        if external_runner is not None:
            try:
                output = external_runner(prompt, self.workdir, env, timeout)
            except TimeoutError:
                raise
            except Exception as exc:
                detail = str(exc)
                raise CliExecutionError(detail, stderr=detail) from exc
            terminal = detect_terminal_backend_message(output)
            if terminal:
                raise CliExecutionError(terminal, stdout=output)
            return output

        use_stdin = bool(self.spec.stdin_prefix)
        cmd = (
            list(self.spec.stdin_prefix)
            if use_stdin
            else self.build_command(prompt)
        )

        try:
            # start_new_session puts the CLI into its own process group so a
            # timeout can stop the whole tree, including MCP servers and other
            # helper processes the CLI spawned.
            proc = subprocess.Popen(
                cmd,
                cwd=self.workdir,
                stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            detail = str(exc)
            if exc.errno == 7 and not self.spec.stdin_prefix:
                detail += (
                    f" (prompt exceeds argv limits and backend "
                    f"{self.ai_cli} has no stdin fallback)"
                )
            raise CliExecutionError(detail, stderr=detail) from exc

        with proc:
            try:
                stdout, stderr = proc.communicate(
                    prompt if use_stdin else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                _stop_process_group(proc)
                proc.communicate()
                raise TimeoutError(str(exc)) from exc

        if proc.returncode != 0:
            structured_error = ""
            if self.spec.structured_output:
                structured_error = recover_cli_error_from_raw_stream(
                    self.ai_cli, stdout
                )
            error_text = (
                structured_error
                or stderr.strip()
                or stdout.strip()
                or "CLI execution failed"
            )
            raise CliExecutionError(
                error_text,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        return self._decode_stdout(stdout)
