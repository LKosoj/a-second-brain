"""Installation and runtime diagnostics."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TextIO

from dotenv import dotenv_values

from d_brain.control_plane.registry import validate_control_plane_registry
from d_brain.manifest import ManifestValidationError, VaultManifest, load_manifest

CheckLevel = Literal["OK", "INFO", "WARN", "ERR"]
SUPPORTED_AI_CLIS = frozenset(
    {"claude", "claude-tmux", "codex", "qwen", "gemini", "kimi", "grok", "opencode"}
)
# Backends whose executable name differs from the AI_CLI value.
AI_CLI_BINARIES = {"claude-tmux": "claude"}


@dataclass(frozen=True)
class DoctorCheck:
    """One human-readable diagnostic result."""

    level: CheckLevel
    message: str


@dataclass
class DoctorReport:
    """Collected results for one project inspection."""

    project_dir: Path
    checks: list[DoctorCheck] = field(default_factory=list)

    def add(self, level: CheckLevel, message: str) -> None:
        self.checks.append(DoctorCheck(level, message))

    @property
    def error_count(self) -> int:
        return sum(check.level == "ERR" for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.level == "WARN" for check in self.checks)

    def render(self, stream: TextIO) -> None:
        print("Doctor for A Second Brain", file=stream)
        print(f"Project: {self.project_dir}", file=stream)
        print(file=stream)
        for check in self.checks:
            print(f"[{check.level}] {check.message}", file=stream)
        print(file=stream)
        outcome = "PASS" if self.error_count == 0 else "FAIL"
        print(
            f"Result: {outcome} "
            f"({self.error_count} errors, {self.warning_count} warnings)",
            file=stream,
        )


class ProjectDoctor:
    """Run deterministic checks without exposing configuration values."""

    def __init__(
        self,
        project_dir: Path,
        *,
        smoke: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.smoke = smoke
        self.process_environ = dict(os.environ if environ is None else environ)
        self.report = DoctorReport(self.project_dir)
        self.values: dict[str, str] = {}
        self.manifest: VaultManifest | None = None
        self.vault_path = self.project_dir / "vault"
        self.ai_cli = "claude"
        self.ai_cli_installed = False

    def run(self) -> DoctorReport:
        self._load_environment()
        self._check_environment()
        self._check_project_files()
        self._check_control_plane_registry()
        self._check_commands()
        self._check_integrations()
        self._check_backup()
        if self.smoke:
            self._run_smoke()
        return self.report

    def _load_environment(self) -> None:
        env_path = self.project_dir / ".env"
        if env_path.is_file():
            self.report.add("OK", f".env exists at {env_path}")
            try:
                file_values = dotenv_values(env_path)
            except (OSError, ValueError):
                self.report.add("ERR", ".env could not be parsed")
                file_values = {}
            self.values.update(
                {
                    str(key): str(value)
                    for key, value in file_values.items()
                    if value is not None
                }
            )
            mode = stat.S_IMODE(env_path.stat().st_mode)
            if mode & 0o077:
                self.report.add(
                    "WARN",
                    f".env permissions are {mode:o}; use 600 to protect secrets",
                )
            else:
                self.report.add("OK", ".env permissions restrict group and others")
        else:
            self.report.add("ERR", f".env is missing at {env_path}")

        self.values.update(self.process_environ)

    def _check_environment(self) -> None:
        for name in ("TELEGRAM_BOT_TOKEN", "DEEPGRAM_API_KEY"):
            if self._value(name):
                self.report.add("OK", f"{name} is set")
            else:
                self.report.add("ERR", f"{name} is missing")

        owner_id = self._value("OWNER_TELEGRAM_ID")
        if owner_id.isdigit() and int(owner_id) > 0:
            self.report.add("OK", "OWNER_TELEGRAM_ID is a positive integer")
        else:
            self.report.add("ERR", "OWNER_TELEGRAM_ID must be a positive integer")

        self.ai_cli = self._value("AI_CLI") or "claude"
        if self.ai_cli in SUPPORTED_AI_CLIS:
            self.report.add("OK", f"AI_CLI selects '{self.ai_cli}'")
        else:
            supported = ", ".join(sorted(SUPPORTED_AI_CLIS))
            self.report.add(
                "ERR",
                f"AI_CLI '{self.ai_cli}' is unsupported; choose one of: {supported}",
            )

        if self._value("OWNER_FULL_NAME"):
            self.report.add("OK", "OWNER_FULL_NAME is set")
        else:
            self.report.add(
                "INFO",
                "OWNER_FULL_NAME is not set; ownership prompts use fallback wording",
            )

    def _check_project_files(self) -> None:
        raw_vault_path = self._value("VAULT_PATH") or "./vault"
        candidate = Path(raw_vault_path).expanduser()
        self.vault_path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.project_dir / candidate).resolve()
        )
        if self.vault_path.is_dir():
            self.report.add("OK", f"Private vault exists at {self.vault_path}")
        else:
            self.report.add("ERR", f"Private vault is missing at {self.vault_path}")

        try:
            self.manifest = load_manifest(self.project_dir)
        except ManifestValidationError as exc:
            self.report.add("ERR", str(exc))
        else:
            self.report.add("OK", "vault-manifest.json is valid")
            declared_vault = (
                self.project_dir / self.manifest.memory_root
            ).resolve()
            if declared_vault != self.vault_path:
                self.report.add(
                    "ERR",
                    "VAULT_PATH does not match vault-manifest.json memory_root",
                )

        mcp_path = self.project_dir / "mcp-config.json"
        try:
            mcp_payload = json.loads(mcp_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.report.add("ERR", f"mcp-config.json is missing at {mcp_path}")
        except (OSError, json.JSONDecodeError):
            self.report.add("ERR", "mcp-config.json is not valid readable JSON")
        else:
            servers = (
                mcp_payload.get("mcpServers")
                if isinstance(mcp_payload, dict)
                else None
            )
            if isinstance(servers, dict):
                self.report.add("OK", "mcp-config.json is valid")
            else:
                self.report.add("ERR", "mcp-config.json must define mcpServers")

    def _check_control_plane_registry(self) -> None:
        try:
            errors = validate_control_plane_registry()
        except Exception as exc:
            self.report.add("ERR", f"Control-plane registry: {exc}")
            return
        if errors:
            for error in errors:
                self.report.add("ERR", f"Control-plane registry: {error}")
        else:
            self.report.add("OK", "Control-plane workflow registry is valid")

    def _check_commands(self) -> None:
        for command in ("uv", "jq"):
            if shutil.which(command):
                self.report.add("OK", f"Required command '{command}' is installed")
            else:
                self.report.add("ERR", f"Required command '{command}' is missing")

        if self.ai_cli not in SUPPORTED_AI_CLIS:
            return
        binary = AI_CLI_BINARIES.get(self.ai_cli, self.ai_cli)
        self.ai_cli_installed = shutil.which(binary) is not None
        if not self.ai_cli_installed:
            self.report.add("ERR", f"AI CLI '{self.ai_cli}' is missing")
            return
        self.report.add("OK", f"AI CLI '{self.ai_cli}' is installed")
        if self.ai_cli == "claude-tmux" and not shutil.which("tmux"):
            self.ai_cli_installed = False
            self.report.add(
                "ERR", "AI CLI 'claude-tmux' requires tmux, which is missing"
            )
            return
        if self._auth_ready():
            self.report.add("OK", f"Authentication for '{self.ai_cli}' is ready")
        else:
            self.report.add(
                "WARN",
                f"Authentication for '{self.ai_cli}' was not confirmed; "
                f"run: {self._auth_hint()}",
            )

    def _check_integrations(self) -> None:
        if self._value("TODOIST_API_KEY"):
            missing = [
                command for command in ("mcp-cli", "npx") if not shutil.which(command)
            ]
            if missing:
                self.report.add(
                    "ERR",
                    "Todoist is configured but required commands are missing: "
                    + ", ".join(missing),
                )
            else:
                self.report.add("OK", "Todoist prerequisites are installed")
        else:
            self.report.add("INFO", "Todoist integration is not configured")

        if shutil.which("qmd"):
            self.report.add("OK", "QMD command is installed")
        else:
            self.report.add("INFO", "QMD command is not installed")

        if self._value("PLAUD_BEARER_TOKEN"):
            self.report.add("OK", "PLAUD integration is configured")
        else:
            self.report.add("INFO", "PLAUD integration is not configured")

    def _check_backup(self) -> None:
        recipient = self._value("VAULT_BACKUP_GPG_RECIPIENT")
        if not recipient:
            self.report.add("INFO", "Encrypted vault backups are not configured")
            return

        if shutil.which("gpg"):
            self.report.add("OK", "GPG command is installed for encrypted backups")
        else:
            self.report.add(
                "ERR",
                "VAULT_BACKUP_GPG_RECIPIENT is set but gpg is missing",
            )

        raw_backup_path = self._value("VAULT_BACKUP_DIR") or "./.vault-backups"
        candidate = Path(raw_backup_path).expanduser()
        backup_path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.project_dir / candidate).resolve()
        )
        if backup_path == self.vault_path or backup_path.is_relative_to(
            self.vault_path
        ):
            self.report.add("ERR", "VAULT_BACKUP_DIR must be outside VAULT_PATH")
        else:
            self.report.add("OK", "VAULT_BACKUP_DIR is outside the private vault")

    def _auth_ready(self) -> bool:
        if self.ai_cli == "gemini":
            return any(
                self._value(name)
                for name in (
                    "GOOGLE_API_KEY",
                    "GEMINI_API_KEY",
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "GOOGLE_GENAI_USE_VERTEXAI",
                )
            )
        if self.ai_cli == "grok":
            return bool(self._value("XAI_API_KEY"))
        if self.ai_cli in {"kimi", "opencode"}:
            return False

        commands = {
            "claude": (["claude", "auth", "status"], '"loggedIn": true'),
            "claude-tmux": (["claude", "auth", "status"], '"loggedIn": true'),
            "codex": (["codex", "login", "status"], "Logged in"),
            "qwen": (["qwen", "auth", "status"], "Authentication Method"),
        }
        command, marker = commands[self.ai_cli]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=self.values,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and marker in f"{result.stdout}\n{result.stderr}"

    def _auth_hint(self) -> str:
        return {
            "claude": "claude auth login",
            "claude-tmux": "claude auth login",
            "codex": "codex login",
            "qwen": "qwen auth qwen-oauth",
            "gemini": "configure GOOGLE_API_KEY or GEMINI_API_KEY",
            "kimi": "kimi login",
            "grok": "configure XAI_API_KEY",
            "opencode": "opencode auth login",
        }.get(self.ai_cli, "configure the selected AI CLI")

    def _run_smoke(self) -> None:
        if self.ai_cli not in SUPPORTED_AI_CLIS or not self.ai_cli_installed:
            self.report.add(
                "ERR",
                "Smoke test could not run because AI_CLI is unavailable",
            )
            return

        # The TUI backend spends its first seconds booting claude's interface.
        timeout = 60 if self.ai_cli == "claude-tmux" else 20
        command = [
            sys.executable,
            "-m",
            "d_brain.run_agent",
            "--ai-cli",
            self.ai_cli,
            "--workdir",
            str(self.project_dir),
            "--timeout",
            str(timeout),
            "--prompt",
            "Reply with exactly OK",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout + 10,
                cwd=self.project_dir,
                env=self.values,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.report.add("ERR", "Smoke test failed to execute")
            return
        if result.returncode == 0 and result.stdout.strip() == "OK":
            self.report.add("OK", "AI CLI smoke test passed")
        else:
            self.report.add(
                "ERR",
                f"AI CLI smoke test failed with exit code {result.returncode}",
            )

    def _value(self, name: str) -> str:
        return self.values.get(name, "").strip()


def run_doctor(
    project_dir: Path,
    *,
    smoke: bool = False,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> int:
    """Inspect one project and return a process-compatible status code."""

    report = ProjectDoctor(project_dir, smoke=smoke, environ=environ).run()
    report.render(sys.stdout if stream is None else stream)
    return 1 if report.error_count else 0
