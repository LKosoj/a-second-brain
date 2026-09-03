"""Small multi-CLI runner for agent backends."""

from __future__ import annotations

import logging
import os
import re
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

logger = logging.getLogger(__name__)

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
_QUOTA_MARKERS = (
    "quota exceeded",
    "daily quota has been reached",
    "rate limit exceeded",
)
_MAX_TERMINAL_MESSAGE_LENGTH = 200

# Exact-name allowlist for `build_subprocess_env`: vars that are neither a
# CLI's own namespace nor safe to match by prefix alone.
_ENV_ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "TMPDIR",
        "TZ",
        "EXTRA_PATH",
        "MCP_CONFIG_PATH",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

# Prefix allowlist for `build_subprocess_env`. Each AI CLI's own auth/config
# namespace, verified against docs/*/configuration.md, docs/*/integrations.md
# and doctor.py's `_auth_ready` checks: Gemini authenticates through
# `GOOGLE_*` vars (GOOGLE_API_KEY, GOOGLE_APPLICATION_CREDENTIALS,
# GOOGLE_GENAI_USE_VERTEXAI), not only `GEMINI_API_KEY`, and Grok through
# `XAI_API_KEY`, not a `GROK_*` var. `NODE_*`/`NPM_CONFIG_*` cover the
# `npx`-launched Todoist MCP server.
_ENV_ALLOWED_PREFIXES = (
    "LC_",
    "XDG_",
    "UV_",
    "CLAUDE_",
    "CODEX_",
    "GEMINI_",
    "GOOGLE_",
    "QWEN_",
    "KIMI_",
    "GROK_",
    "XAI_",
    "OPENCODE_",
    "ANTHROPIC_",
    "NODE_",
    "NPM_CONFIG_",
)


def build_subprocess_env(
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment for AI CLI subprocesses.

    Systemd feeds this process the full ``.env`` -- the Telegram bot token,
    Deepgram/PLAUD/search-provider keys, and more -- while AI CLIs run with
    ``--dangerously-skip-permissions`` or an equivalent flag. A prompt
    injected into one of them could otherwise read and exfiltrate every
    secret this process holds, not just the one the backend actually needs.
    Start from an allowlist of the vars a CLI, ``mcp-cli``, and ``npx``
    genuinely use, then layer any explicit ``extra_env`` on top so callers
    can still hand a backend the one secret it needs (e.g. TODOIST_API_KEY).
    """

    env = {
        name: value
        for name, value in os.environ.items()
        if name in _ENV_ALLOWED_NAMES or name.startswith(_ENV_ALLOWED_PREFIXES)
    }
    if extra_env:
        for key, value in extra_env.items():
            if value:
                env[key] = value
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    return env


@dataclass(frozen=True)
class CliSpec:
    """Static command template for one supported CLI."""

    name: AiCliName
    argv_prefix: tuple[str, ...]
    stdin_prefix: tuple[str, ...] = field(default=())
    structured_output: bool = False
    # Alternate prefix used only when a caller asks for ``restricted``
    # execution (the unattended scheduled cycle: no shell, no network).
    # ``None`` means this backend has no known tool-restriction flag, so
    # ``restricted=True`` silently falls back to the normal prefix instead
    # of inventing one.
    restricted_argv_prefix: tuple[str, ...] | None = None
    restricted_stdin_prefix: tuple[str, ...] | None = None


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
        # `--disallowedTools` (verified against `claude --help`) denies
        # shell and outbound-fetch tools for the unattended scheduled
        # cycle while leaving file/vault access intact.
        restricted_argv_prefix=(
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--disallowedTools",
            "Bash,WebFetch,WebSearch",
        ),
        restricted_stdin_prefix=(
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--disallowedTools",
            "Bash,WebFetch,WebSearch",
        ),
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
        # `--dangerously-bypass-approvals-and-sandbox` (above) disables
        # sandboxing outright, so restricted mode must drop it rather than
        # add to it. `--sandbox workspace-write` (verified against `codex
        # exec --help`) still allows writes under the vault, and per
        # Codex's docs that sandbox keeps outbound network access off
        # unless `sandbox_workspace_write.network_access` is set in config,
        # which this runner never does.
        restricted_argv_prefix=(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--json",
        ),
        restricted_stdin_prefix=(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--json",
        ),
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
    return normalized


_PROCESS_STOP_GRACE_SECONDS = 5.0


def _stop_process_group(
    proc: subprocess.Popen[str], *, grace: float = _PROCESS_STOP_GRACE_SECONDS
) -> None:
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


def _terminal_message_head(raw_lower: str) -> str:
    """First line or first sentence of a message, whichever ends first.

    Whitespace inside that head is collapsed to single spaces, matching the
    normalized form markers are matched against.
    """

    match = re.search(r"[.\n!?]", raw_lower)
    end = match.start() if match else len(raw_lower)
    return " ".join(raw_lower[:end].split())


def _quota_marker_is_terminal(raw_lower: str, normalized: str, marker: str) -> bool:
    """Whether a quota/rate-limit marker match is a raw backend message.

    This only guards text recovered as a CLI's *successful-looking assistant
    answer* (``_decode_stdout`` and the external-runner output in
    ``CliRunner.run``) -- stderr and structured JSON errors (``is_error``,
    ``subtype: error``, an ``error`` field) never reach this check at all:
    ``CliRunner.run`` raises on those unconditionally before this function is
    ever called, so a genuinely long, multi-sentence backend error is never
    silently dropped just because it is verbose.

    For answer text, a match is terminal when the whole message is short, or
    when the marker sits in the message's head (its first line or first
    sentence, whichever ends first) *and* that same head starts with an
    "error:"/"error " prefix or carries an HTTP 429 code -- the shapes a raw
    backend message takes. Both the marker and the "error"/"429" signal must
    live in that head: a longer answer that merely mentions handling HTTP 429
    responses in one sentence and, separately, explains what a quota is in
    another is left alone, so it is not dropped as an error.
    """

    if len(normalized) <= _MAX_TERMINAL_MESSAGE_LENGTH:
        return True
    head = _terminal_message_head(raw_lower)
    if marker not in head:
        return False
    return head.startswith(("error:", "error ")) or "429" in head


def detect_terminal_backend_message(text: str) -> str | None:
    (
        "Return the original message when backend text signals a terminal "
        "execution failure."
    )

    raw = str(text or "").strip()
    raw_lower = raw.lower()
    normalized = " ".join(raw_lower.split())
    if not normalized:
        return None
    if "too many requests" in normalized and not (
        normalized == "too many requests"
        or normalized.startswith("error")
        or "429" in normalized
        or "quota" in normalized
    ):
        return None
    quota_hits = [marker for marker in _QUOTA_MARKERS if marker in normalized]
    if quota_hits and not any(
        _quota_marker_is_terminal(raw_lower, normalized, marker)
        for marker in quota_hits
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

    def _warn_if_restricted_unsupported(
        self, restricted: bool, *, has_restricted_variant: bool
    ) -> None:
        """Log once when ``restricted=True`` has nothing to change for this backend."""
        if restricted and not has_restricted_variant:
            logger.warning(
                "CLI backend %s has no restricted mode; nightly cycle runs "
                "unrestricted",
                self.ai_cli,
            )

    def build_command(self, prompt: str, *, restricted: bool = False) -> list[str]:
        """Build argv for backend execution.

        ``restricted=True`` is for the unattended scheduled cycle: it swaps
        in the backend's shell/network-denying prefix when one is declared
        (see ``CliSpec.restricted_argv_prefix``). Only claude and codex
        currently declare one; every other backend (qwen, gemini, kimi, grok,
        opencode, claude-tmux) has no such flag, so ``restricted=True`` is a
        no-op for them and logs a warning instead of silently running
        unrestricted.
        """

        if self.ai_cli in _EXTERNAL_RUNNERS:
            self._warn_if_restricted_unsupported(
                restricted, has_restricted_variant=False
            )
            return list(self.spec.argv_prefix)
        prefix = self.spec.argv_prefix
        restricted_prefix = self.spec.restricted_argv_prefix
        if restricted and restricted_prefix is not None:
            prefix = restricted_prefix
        self._warn_if_restricted_unsupported(
            restricted, has_restricted_variant=restricted_prefix is not None
        )
        return [*prefix, prompt]

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
        restricted: bool = False,
    ) -> str:
        """Execute prompt and return stdout text.

        ``restricted=True`` is for the unattended scheduled cycle -- see
        ``build_command`` for which backends (claude, codex) actually declare
        a restricted-mode flag. External runners (kimi, claude-tmux) and the
        remaining backends (qwen, gemini, grok, opencode) have no argv to
        restrict here, are left untouched, and log a warning instead.
        """

        env = build_subprocess_env(extra_env)

        external_runner = _EXTERNAL_RUNNERS.get(self.ai_cli)
        if external_runner is not None:
            self._warn_if_restricted_unsupported(
                restricted, has_restricted_variant=False
            )
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
        if use_stdin:
            stdin_prefix = self.spec.stdin_prefix
            restricted_stdin_prefix = self.spec.restricted_stdin_prefix
            if restricted and restricted_stdin_prefix is not None:
                stdin_prefix = restricted_stdin_prefix
            self._warn_if_restricted_unsupported(
                restricted, has_restricted_variant=restricted_stdin_prefix is not None
            )
            cmd = list(stdin_prefix)
        else:
            cmd = self.build_command(prompt, restricted=restricted)

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
                try:
                    # Drain the pipes so the process can be reaped, but bound
                    # the wait: a grandchild in another process group (e.g. an
                    # MCP server the CLI spawned) can keep holding stdout open
                    # even after the CLI itself is gone, which would hang an
                    # untimed communicate() forever.
                    proc.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        # A grandchild is still holding a pipe open even
                        # after SIGKILL to the CLI itself. The output is
                        # discarded on this path anyway, so stop waiting
                        # for it instead of risking an indefinite hang.
                        if proc.stdout:
                            proc.stdout.close()
                        if proc.stderr:
                            proc.stderr.close()
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
