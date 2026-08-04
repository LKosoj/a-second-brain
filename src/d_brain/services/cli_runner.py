"""Small multi-CLI runner for agent backends."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from d_brain.services.cli_json_stream import (
    recover_cli_error_from_raw_stream,
    recover_cli_text_from_raw_stream,
)

AiCliName = Literal["claude", "codex", "qwen", "gemini"]

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
        argv_prefix=(
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "-p",
        ),
        stdin_prefix=(
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "-p",
        ),
        structured_output=True,
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
        """Build argv for one-shot execution."""

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

        use_stdin = bool(self.spec.stdin_prefix)
        cmd = (
            list(self.spec.stdin_prefix)
            if use_stdin
            else self.build_command(prompt)
        )

        try:
            result = subprocess.run(
                cmd,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
                input=prompt if use_stdin else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        except OSError as exc:
            detail = str(exc)
            if exc.errno == 7 and not self.spec.stdin_prefix:
                detail += (
                    f" (prompt exceeds argv limits and backend "
                    f"{self.ai_cli} has no stdin fallback)"
                )
            raise CliExecutionError(detail, stderr=detail) from exc
        if result.returncode != 0:
            structured_error = ""
            if self.spec.structured_output:
                structured_error = recover_cli_error_from_raw_stream(
                    self.ai_cli, result.stdout
                )
            error_text = (
                structured_error
                or result.stderr.strip()
                or result.stdout.strip()
                or "CLI execution failed"
            )
            raise CliExecutionError(
                error_text,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        return self._decode_stdout(result.stdout)
