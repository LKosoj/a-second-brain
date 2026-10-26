"""Installation and runtime diagnostics."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from d_brain.clock import resolve_timezone, system_timezone_name
from d_brain.config import parse_clock_time
from d_brain.control_plane.registry import validate_control_plane_registry
from d_brain.manifest import ManifestValidationError, VaultManifest, load_manifest
from d_brain.services.web_content import is_public_ip

CheckLevel = Literal["OK", "INFO", "WARN", "ERR"]
SUPPORTED_AI_CLIS = frozenset(
    {
        "claude",
        "claude-tmux",
        "codex",
        "codex-tmux",
        "qwen",
        "gemini",
        "kimi",
        "grok",
        "opencode",
    }
)
# Backends whose executable name differs from the AI_CLI value.
AI_CLI_BINARIES = {"claude-tmux": "claude", "codex-tmux": "codex"}
# Setting names doctor looks up. pydantic-settings reads .env keys
# case-insensitively; a lowercase variant of one of these still works at
# runtime, but is worth flagging since not every consumer of .env
# (shell scripts, systemd's EnvironmentFile) is as forgiving.
KNOWN_ENV_KEYS = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "DEEPGRAM_API_KEY",
        "OWNER_TELEGRAM_ID",
        "AI_CLI",
        "OWNER_FULL_NAME",
        "TODOIST_API_KEY",
        "PLAUD_BEARER_TOKEN",
        "PLAUD_REGION",
        "CALENDAR_ICS_URL",
        "VAULT_BACKUP_GPG_RECIPIENT",
        "VAULT_BACKUP_DIR",
        "VAULT_PATH",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "MORNING_BRIEF_TIME",
        "DAILY_PROCESS_TIME",
    }
)
# (outcome, detail): detail is the bot username or an exception class name,
# never anything that can carry the token.
TelegramProbe = tuple[Literal["ok", "rejected", "unreachable"], str]
LEGACY_OPENAI_NAMES = (("BASE_URL", "OPENAI_BASE_URL"), ("MODEL", "OPENAI_MODEL"))
SCHEDULE_TIME_KEYS = ("MORNING_BRIEF_TIME", "DAILY_PROCESS_TIME")
SUPPORTED_PLAUD_REGIONS = frozenset({"api", "api-euc1"})
_ENV_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _probe_telegram(token: str) -> TelegramProbe:
    """Ask Telegram ``getMe`` whether the bot token is accepted.

    httpx puts the request URL (with the token) into its exception messages,
    so only the exception class name may leave this function.
    """
    try:
        response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5.0)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return "unreachable", type(exc).__name__
    if response.status_code in (401, 404):
        return "rejected", ""
    if response.status_code != 200:
        return "unreachable", f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return "unreachable", "InvalidJSON"
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return "unreachable", "UnexpectedResponse"
    result = payload.get("result")
    username = result.get("username") if isinstance(result, dict) else None
    return "ok", f"@{username}" if isinstance(username, str) and username else "bot"


def _first_symlink_component(path: Path) -> Path | None:
    """Return the first symlinked path component of an absolute path, if any.

    ``vault_lock.open_vault_root_nofollow`` opens every directory component
    with ``O_NOFOLLOW`` and refuses to proceed past a symlink, so a symlink
    anywhere in the vault path breaks vault writes at runtime.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _has_unquoted_risky_value(raw_value: str) -> bool:
    """Whether a raw (unparsed) .env value has an unquoted space or ``$``.

    python-dotenv parses these values without complaint, but a value like
    this breaks naive shell-based consumers of the same file.
    """
    value = raw_value.strip()
    if value[:1] in ("\"", "'"):
        closing = value.find(value[0], 1)
        # A quoted value may be followed only by an inline comment.
        if closing != -1 and value[closing + 1 :].lstrip()[:1] in ("", "#"):
            return False
    value = value.split(" #", 1)[0].rstrip()
    return " " in value or "$" in value


def _system_utc_offset() -> timedelta | None:
    return datetime.now().astimezone().utcoffset()


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
        telegram_probe: Callable[[str], TelegramProbe] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.smoke = smoke
        self.process_environ = dict(os.environ if environ is None else environ)
        # Network only when run from the CLI (no explicit environ), so tests
        # and embedded calls with a fixed environment stay offline.
        if telegram_probe is None and environ is None:
            telegram_probe = _probe_telegram
        self.telegram_probe = telegram_probe
        self.report = DoctorReport(self.project_dir)
        self.values: dict[str, str] = {}
        self.manifest: VaultManifest | None = None
        self.vault_path = self.project_dir / "vault"
        self.ai_cli = "claude"
        self.ai_cli_installed = False

    def run(self) -> DoctorReport:
        self._load_environment()
        self._check_environment()
        self._check_telegram_token()
        self._check_schedule_times()
        self._check_timezone()
        self._check_openai_names()
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
            for key in file_values:
                if key != key.upper() and key.upper() in KNOWN_ENV_KEYS:
                    self.report.add(
                        "WARN",
                        f".env key '{key}' should be uppercase "
                        f"'{key.upper()}'; pydantic-settings reads env keys "
                        "case-insensitively but not every tool does",
                    )
            self.values.update(
                {
                    str(key): str(value)
                    for key, value in file_values.items()
                    if value is not None
                }
            )
            self._check_raw_env_lines(env_path)
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

    def _check_raw_env_lines(self, env_path: Path) -> None:
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            return
        flagged: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_ASSIGNMENT_RE.match(stripped)
            if match is None:
                continue
            key, raw_value = match.group(1), match.group(2)
            if _has_unquoted_risky_value(raw_value) and key not in flagged:
                flagged.append(key)
        for key in flagged:
            self.report.add(
                "WARN",
                f"{key} in .env has an unquoted space or '$'; quote the "
                "value so shell-based tools (a manual `source .env`, "
                "ad-hoc scripts) parse it correctly",
            )

    def _check_environment(self) -> None:
        if self._value("TELEGRAM_BOT_TOKEN"):
            self.report.add("OK", "TELEGRAM_BOT_TOKEN is set")
        else:
            self.report.add("ERR", "TELEGRAM_BOT_TOKEN is missing")

        if self._value("DEEPGRAM_API_KEY"):
            self.report.add("OK", "DEEPGRAM_API_KEY is set")
        else:
            self.report.add(
                "WARN",
                "DEEPGRAM_API_KEY is not set; voice messages are saved as audio "
                "marked 'не распознано' and transcribed after the key is added",
            )

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

    def _check_telegram_token(self) -> None:
        token = self._value("TELEGRAM_BOT_TOKEN")
        if not token or self.telegram_probe is None:
            return
        outcome, detail = self.telegram_probe(token)
        if outcome == "ok":
            self.report.add("OK", f"Telegram accepted TELEGRAM_BOT_TOKEN ({detail})")
        elif outcome == "rejected":
            self.report.add("ERR", "Telegram rejected TELEGRAM_BOT_TOKEN")
        else:
            self.report.add(
                "WARN",
                f"Could not reach Telegram to verify TELEGRAM_BOT_TOKEN "
                f"({detail}); check skipped",
            )

    def _check_schedule_times(self) -> None:
        for name in SCHEDULE_TIME_KEYS:
            value = self._value(name)
            if value and parse_clock_time(value) is None:
                self.report.add(
                    "WARN",
                    f"{name} must be HH:MM (00:00-23:59); the installer will "
                    "refuse it and the default time applies at runtime",
                )
            elif name == "DAILY_PROCESS_TIME" and value:
                parsed = parse_clock_time(value)
                if parsed is not None and parsed[0] < 6:
                    self.report.add(
                        "WARN",
                        f"{name} {value.strip()} is in the small hours; a run "
                        "before that time is treated as catch-up for the "
                        "previous day, and a run just after midnight sees an "
                        "empty new day. Pick an evening time",
                    )

    def _check_timezone(self) -> None:
        value = self._value("TIMEZONE")
        system_name = system_timezone_name()
        if not value:
            self.report.add(
                "OK",
                f"TIMEZONE is not set; the system zone {system_name} "
                "defines the day",
            )
            return
        try:
            zone = resolve_timezone(value)
        except ValueError:
            self.report.add(
                "ERR", "TIMEZONE must be an IANA name such as Europe/Moscow"
            )
            return
        owner_offset = datetime.now(zone).utcoffset()
        if owner_offset == _system_utc_offset():
            self.report.add(
                "OK", f"TIMEZONE {value} matches the system zone {system_name}"
            )
            return
        self.report.add(
            "WARN",
            f"TIMEZONE {value} differs from the system zone {system_name}: "
            "entries and reminders follow TIMEZONE, systemd timers carry it "
            "from the installer, but launchd runs by the system clock, so the "
            "installer converts the hours. Re-run the installer after a "
            "daylight-saving change or any TIMEZONE change",
        )

    def _check_openai_names(self) -> None:
        for legacy, current in LEGACY_OPENAI_NAMES:
            old, new = self._value(legacy), self._value(current)
            if old and not new:
                self.report.add(
                    "WARN",
                    f"{legacy} is the legacy name; rename it to {current} "
                    "(the old name still works)",
                )
            elif old and new and old != new:
                self.report.add(
                    "WARN",
                    f"both {legacy} and {current} are set; {current} wins",
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

        raw_absolute = (
            candidate if candidate.is_absolute() else (self.project_dir / candidate)
        )
        symlinked = _first_symlink_component(raw_absolute)
        if symlinked is not None:
            self.report.add(
                "WARN",
                f"vault path contains a symlink at {symlinked}; vault_lock "
                "refuses symlinked path components and will error at runtime",
            )

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
        if self.ai_cli in {"claude-tmux", "codex-tmux"} and not shutil.which("tmux"):
            self.ai_cli_installed = False
            self.report.add(
                "ERR", f"AI CLI '{self.ai_cli}' requires tmux, which is missing"
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

        plaud_region = self._value("PLAUD_REGION")
        if plaud_region and plaud_region not in SUPPORTED_PLAUD_REGIONS:
            supported = ", ".join(sorted(SUPPORTED_PLAUD_REGIONS))
            self.report.add(
                "WARN",
                f"PLAUD_REGION '{plaud_region}' is not one of: {supported}",
            )

        self._check_calendar()

    def _check_calendar(self) -> None:
        # The link is a secret: no message below repeats it. DNS is not asked
        # (doctor works offline), so only literal addresses and localhost are
        # recognised as non-public.
        url = self._value("CALENDAR_ICS_URL")
        if not url:
            self.report.add("INFO", "Calendar is not configured")
            return
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            schemes = {"http", "https", "webcal", "webcals"}
            valid = parts.scheme.lower() in schemes and bool(host)
        except ValueError:
            valid = False
        if not valid:
            self.report.add(
                "ERR", "CALENDAR_ICS_URL must be an http(s) or webcal link"
            )
            return
        try:
            public = is_public_ip(host)
        except ValueError:
            public = host != "localhost" and not host.endswith(".localhost")
        if not public:
            self.report.add(
                "WARN",
                "CALENDAR_ICS_URL points to a non-public address; "
                "the calendar will be skipped",
            )
            return
        self.report.add("OK", "Calendar is configured")

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
            "codex-tmux": (["codex", "login", "status"], "Logged in"),
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
            "codex-tmux": "codex login",
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
        timeout = 60 if self.ai_cli in {"claude-tmux", "codex-tmux"} else 20
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
        if name in self.values:
            return self.values[name].strip()
        # pydantic-settings reads .env keys case-insensitively; fall back to
        # a case-insensitive lookup so a lowercase key is not misreported as
        # missing (see the .env key-casing WARN in _load_environment).
        upper = name.upper()
        for key, value in self.values.items():
            if key.upper() == upper:
                return value.strip()
        return ""


def run_doctor(
    project_dir: Path,
    *,
    smoke: bool = False,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
    telegram_probe: Callable[[str], TelegramProbe] | None = None,
) -> int:
    """Inspect one project and return a process-compatible status code."""

    report = ProjectDoctor(
        project_dir,
        smoke=smoke,
        environ=environ,
        telegram_probe=telegram_probe,
    ).run()
    report.render(sys.stdout if stream is None else stream)
    return 1 if report.error_count else 0
