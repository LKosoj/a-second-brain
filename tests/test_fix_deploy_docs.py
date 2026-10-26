"""Tests for the G9 "deploy-docs" audit fixes.

Covers: deploy/*.in template hardening, the systemd/launchd installer
scripts, the launchd .env wrapper's hand-rolled parser, install.sh's
restored-vault handling, scripts/doctor.sh's --directory flag, doctor.py's
new diagnostics, and the documentation drift they touch.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from d_brain import doctor
from d_brain.cli import initialize_project

REPO_ROOT = Path(__file__).resolve().parent.parent


def _valid_environment() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "DEEPGRAM_API_KEY": "deepgram-secret",
        "OWNER_TELEGRAM_ID": "123456",
        "AI_CLI": "gemini",
        "GEMINI_API_KEY": "gemini-secret",
    }


def _installed_core_command(command: str) -> str | None:
    if command in {"uv", "jq", "gemini"}:
        return f"/usr/bin/{command}"
    return None


def _write_stub(bin_dir: Path, name: str, body: str = "exit 0\n") -> None:
    script = bin_dir / name
    script.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    script.chmod(0o700)


# ---------------------------------------------------------------------------
# A) deploy/*.in template hardening
# ---------------------------------------------------------------------------


def test_process_service_has_timeout_start_sec() -> None:
    content = (REPO_ROOT / "deploy" / "a-second-brain-process.service.in").read_text(
        encoding="utf-8"
    )
    assert "TimeoutStartSec=4h" in content


def test_bot_service_has_kill_mode_mixed_documented() -> None:
    content = (REPO_ROOT / "deploy" / "a-second-brain.service.in").read_text(
        encoding="utf-8"
    )
    assert "KillMode=mixed" in content
    assert "tmux" in content.lower()


def test_plaud_sync_timer_has_randomized_delay() -> None:
    content = (
        REPO_ROOT / "deploy" / "a-second-brain-plaud-sync.timer.in"
    ).read_text(encoding="utf-8")
    assert "RandomizedDelaySec=300" in content


def test_process_plist_runs_backup_before_daily_process() -> None:
    content = (REPO_ROOT / "deploy" / "com.second-brain.process.plist.in").read_text(
        encoding="utf-8"
    )
    assert "d_brain.run_vault_backup" in content
    assert "d_brain.run_daily_process" in content
    assert content.index("d_brain.run_vault_backup") < content.index(
        "d_brain.run_daily_process"
    )


def test_backup_verify_units_run_weekly_verify() -> None:
    deploy = REPO_ROOT / "deploy"
    service = (deploy / "a-second-brain-backup-verify.service.in").read_text(
        encoding="utf-8"
    )
    timer = (deploy / "a-second-brain-backup-verify.timer.in").read_text(
        encoding="utf-8"
    )
    plist = (deploy / "com.second-brain.backup-verify.plist.in").read_text(
        encoding="utf-8"
    )
    assert "d_brain.run_vault_backup --verify" in service
    assert "OnCalendar=Sun" in timer
    assert "<string>--verify</string>" in plist
    assert "<key>UV_BIN</key>" in plist


def test_all_launchd_plists_export_uv_bin() -> None:
    for name in (
        "com.second-brain.bot.plist.in",
        "com.second-brain.process.plist.in",
        "com.second-brain.morning-brief.plist.in",
        "com.second-brain.plaud-sync.plist.in",
        "com.second-brain.qmd-maintenance.plist.in",
        "com.second-brain.backup-verify.plist.in",
    ):
        content = (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "<key>UV_BIN</key>" in content
        assert "<string>@UV_BIN@</string>" in content


# ---------------------------------------------------------------------------
# B) scripts/install-systemd-user.sh
# ---------------------------------------------------------------------------

_SYSTEMD_DEPLOY_TEMPLATES = (
    "a-second-brain.service.in",
    "a-second-brain-process.service.in",
    "a-second-brain-process.timer.in",
    "a-second-brain-morning-brief.timer.in",
    "a-second-brain-plaud-sync.service.in",
    "a-second-brain-plaud-sync.timer.in",
    "a-second-brain-qmd-maintenance.service.in",
    "a-second-brain-qmd-maintenance.timer.in",
    "a-second-brain-backup-verify.service.in",
    "a-second-brain-backup-verify.timer.in",
)


def _prepare_systemd_project(project_dir: Path, plaud_line: str | None) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = ["TELEGRAM_BOT_TOKEN=t", "DEEPGRAM_API_KEY=d", "OWNER_TELEGRAM_ID=1"]
    if plaud_line is not None:
        lines.append(plaud_line)
    (project_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (project_dir / "vault").mkdir()

    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir()
    script_dst = scripts_dir / "install-systemd-user.sh"
    shutil.copy(REPO_ROOT / "scripts" / "install-systemd-user.sh", script_dst)
    script_dst.chmod(0o755)

    deploy_dst = project_dir / "deploy"
    deploy_dst.mkdir()
    for name in _SYSTEMD_DEPLOY_TEMPLATES:
        shutil.copy(REPO_ROOT / "deploy" / name, deploy_dst / name)
    # A launchd-only template must never be rendered into a systemd unit dir.
    (deploy_dst / "com.second-brain.bot.plist.in").write_text(
        "<plist>should not render under systemd</plist>\n", encoding="utf-8"
    )


def test_systemd_install_ignores_plist_templates(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_systemd_project(project_dir, plaud_line=None)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "systemctl")

    config_home = tmp_path / "config"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-systemd-user.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    unit_dir = config_home / "systemd" / "user"
    assert (unit_dir / "a-second-brain.service").exists()
    assert (unit_dir / "a-second-brain-process.timer").exists()
    assert not (unit_dir / "com.second-brain.bot.plist").exists()


def _install_systemd(
    tmp_path: Path, extra_env_lines: list[str]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    project_dir = tmp_path / "project"
    _prepare_systemd_project(project_dir, plaud_line=None)
    env_file = project_dir / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "\n".join(extra_env_lines) + "\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "systemctl")
    config_home = tmp_path / "config"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["XDG_CONFIG_HOME"] = str(config_home)
    result = subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-systemd-user.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, config_home / "systemd" / "user"


def test_systemd_install_renders_times_from_env(tmp_path: Path) -> None:
    result, unit_dir = _install_systemd(
        tmp_path, ["DAILY_PROCESS_TIME=06:45", 'MORNING_BRIEF_TIME="07:15"']
    )

    assert result.returncode == 0, result.stderr
    process = (unit_dir / "a-second-brain-process.timer").read_text(encoding="utf-8")
    brief = (unit_dir / "a-second-brain-morning-brief.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 06:45:00" in process
    assert "OnCalendar=*-*-* 07:15:00" in brief


def test_systemd_units_have_no_unreplaced_placeholders(tmp_path: Path) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["PLAUD_BEARER_TOKEN=token"])

    assert result.returncode == 0, result.stderr
    units = [*unit_dir.glob("*.service"), *unit_dir.glob("*.timer")]
    assert units
    for unit in units:
        leftover = re.findall(r"@[A-Z_]+@", unit.read_text(encoding="utf-8"))
        assert not leftover, f"{unit.name}: {leftover}"


def test_systemd_install_keeps_default_times(tmp_path: Path) -> None:
    result, unit_dir = _install_systemd(tmp_path, [])

    assert result.returncode == 0, result.stderr
    process = (unit_dir / "a-second-brain-process.timer").read_text(encoding="utf-8")
    brief = (unit_dir / "a-second-brain-morning-brief.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 21:00:00" in process
    assert "OnCalendar=*-*-* 08:00:00" in brief


def test_systemd_install_adds_owner_timezone_to_both_timers(
    tmp_path: Path,
) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["TIMEZONE=Europe/Moscow"])

    assert result.returncode == 0, result.stderr
    process = (unit_dir / "a-second-brain-process.timer").read_text(encoding="utf-8")
    brief = (unit_dir / "a-second-brain-morning-brief.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 21:00:00 Europe/Moscow\n" in process
    assert "OnCalendar=*-*-* 08:00:00 Europe/Moscow\n" in brief
    assert "@TIMEZONE_SUFFIX@" not in process + brief


def test_systemd_install_without_timezone_keeps_timers_unchanged(
    tmp_path: Path,
) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["TIMEZONE="])

    assert result.returncode == 0, result.stderr
    process = (unit_dir / "a-second-brain-process.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 21:00:00\n" in process
    assert "@TIMEZONE_SUFFIX@" not in process


def test_systemd_services_get_tz_from_timezone(tmp_path: Path) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["TIMEZONE=Europe/Moscow"])

    assert result.returncode == 0, result.stderr
    services = sorted(unit_dir.glob("*.service"))
    assert len(services) == 5
    for service in services:
        body = service.read_text(encoding="utf-8")
        pattern = r"^Environment=.*\bTZ=Europe/Moscow$"
        assert re.search(pattern, body, re.M), service.name
        # systemd lets EnvironmentFile= override Environment=, so a TZ the
        # owner put into .env wins over the derived one instead of clashing.
        assert body.index("EnvironmentFile=") < body.index("TZ=Europe/Moscow")
        assert "UV_BIN=" in body


@pytest.mark.parametrize("lines", [[], ["TIMEZONE="]])
def test_systemd_services_have_no_tz_without_timezone(
    tmp_path: Path, lines: list[str]
) -> None:
    result, unit_dir = _install_systemd(tmp_path, lines)

    assert result.returncode == 0, result.stderr
    for service in unit_dir.glob("*.service"):
        assert "TZ=" not in service.read_text(encoding="utf-8"), service.name


def test_systemd_install_maintenance_timers_stay_on_system_zone(
    tmp_path: Path,
) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["TIMEZONE=Europe/Moscow"])

    assert result.returncode == 0, result.stderr
    for name in sorted(unit_dir.glob("*.timer")):
        if name.name in {
            "a-second-brain-process.timer",
            "a-second-brain-morning-brief.timer",
        }:
            continue
        assert "Europe/Moscow" not in name.read_text(encoding="utf-8"), name.name


@pytest.mark.parametrize("bad", ["Mars/Base", "Europe/Moscow;rm", "../etc/passwd"])
def test_systemd_install_rejects_invalid_timezone(tmp_path: Path, bad: str) -> None:
    result, unit_dir = _install_systemd(tmp_path, [f"TIMEZONE={bad}"])

    assert result.returncode == 1
    assert "TIMEZONE" in result.stderr
    assert not unit_dir.exists() or not list(unit_dir.glob("*.timer"))


def test_systemd_install_rejects_invalid_time(tmp_path: Path) -> None:
    result, unit_dir = _install_systemd(tmp_path, ["DAILY_PROCESS_TIME=25:00"])

    assert result.returncode != 0
    assert "DAILY_PROCESS_TIME" in result.stderr
    assert not unit_dir.exists() or not list(unit_dir.glob("*.timer"))


def test_timer_templates_use_schedule_placeholders() -> None:
    deploy = REPO_ROOT / "deploy"
    process_timer = (deploy / "a-second-brain-process.timer.in").read_text("utf-8")
    brief_timer = (deploy / "a-second-brain-morning-brief.timer.in").read_text(
        "utf-8"
    )
    process_plist = (deploy / "com.second-brain.process.plist.in").read_text("utf-8")
    brief_plist = (deploy / "com.second-brain.morning-brief.plist.in").read_text(
        "utf-8"
    )
    assert "@DAILY_PROCESS_TIME@:00@TIMEZONE_SUFFIX@" in process_timer
    assert "@MORNING_BRIEF_TIME@:00@TIMEZONE_SUFFIX@" in brief_timer
    assert "@DAILY_PROCESS_TIME@" in process_timer
    assert "@MORNING_BRIEF_TIME@" in brief_timer
    for placeholder in ("@DAILY_PROCESS_HOUR@", "@DAILY_PROCESS_MINUTE@"):
        assert placeholder in process_plist
    for placeholder in ("@MORNING_BRIEF_HOUR@", "@MORNING_BRIEF_MINUTE@"):
        assert placeholder in brief_plist


def test_systemd_install_enable_skips_quoted_empty_plaud_token(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    _prepare_systemd_project(project_dir, plaud_line='PLAUD_BEARER_TOKEN=""')

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log_path = tmp_path / "systemctl.log"
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "systemctl", f'echo "$@" >> "{log_path}"\nexit 0\n')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    result = subprocess.run(
        [
            "bash",
            str(project_dir / "scripts" / "install-systemd-user.sh"),
            "--enable",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "a-second-brain.service" in log
    assert "a-second-brain-backup-verify.timer" in log
    assert "a-second-brain-plaud-sync.timer" not in log


@pytest.mark.parametrize(
    "plaud_line",
    ["export PLAUD_BEARER_TOKEN=real-token", "PLAUD_BEARER_TOKEN='real-token'"],
)
def test_systemd_install_enable_accepts_export_prefixed_plaud_token(
    tmp_path: Path, plaud_line: str
) -> None:
    project_dir = tmp_path / "project"
    _prepare_systemd_project(project_dir, plaud_line=plaud_line)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log_path = tmp_path / "systemctl.log"
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "systemctl", f'echo "$@" >> "{log_path}"\nexit 0\n')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    result = subprocess.run(
        [
            "bash",
            str(project_dir / "scripts" / "install-systemd-user.sh"),
            "--enable",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "a-second-brain-plaud-sync.timer" in log_path.read_text(encoding="utf-8")


def test_systemd_install_enable_includes_real_plaud_token(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_systemd_project(
        project_dir, plaud_line="PLAUD_BEARER_TOKEN=real-secret-token"
    )

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log_path = tmp_path / "systemctl.log"
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "systemctl", f'echo "$@" >> "{log_path}"\nexit 0\n')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    result = subprocess.run(
        [
            "bash",
            str(project_dir / "scripts" / "install-systemd-user.sh"),
            "--enable",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "a-second-brain-plaud-sync.timer" in log


# ---------------------------------------------------------------------------
# B) scripts/install-launchd-user.sh -- uninstall must sweep every possible
# label, not just the ones the *current* .env/qmd state would install.
# ---------------------------------------------------------------------------


def test_launchd_uninstall_removes_all_possible_plists(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "scripts" / "install-launchd-user.sh",
        scripts_dir / "install-launchd-user.sh",
    )
    (scripts_dir / "install-launchd-user.sh").chmod(0o755)
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir()
    shutil.copy(
        REPO_ROOT / "scripts" / "lib" / "run_with_env.sh",
        lib_dir / "run_with_env.sh",
    )
    (lib_dir / "run_with_env.sh").chmod(0o755)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "launchctl")

    home = tmp_path / "home"
    agent_dir = home / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True)
    all_labels = (
        "com.second-brain.bot",
        "com.second-brain.process",
        "com.second-brain.plaud-sync",
        "com.second-brain.qmd-maintenance",
        "com.second-brain.backup-verify",
    )
    for label in all_labels:
        (agent_dir / f"{label}.plist").write_text("<plist/>", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

    # No .env and no qmd on PATH: the *conditional* label list the old code
    # reused for --uninstall would only contain bot+process, leaving
    # plaud-sync and qmd-maintenance orphaned.
    result = subprocess.run(
        [
            "bash",
            str(scripts_dir / "install-launchd-user.sh"),
            "--uninstall",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for label in all_labels:
        assert not (agent_dir / f"{label}.plist").exists(), label


# ---------------------------------------------------------------------------
# C) scripts/lib/run_with_env.sh -- safe .env parsing without shell expansion
# ---------------------------------------------------------------------------


def test_run_with_env_parses_without_shell_expansion(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text(
        "A=John Smith\nB='x y'\nC=\"$HOME\"\nD=pa$$w\n",
        encoding="utf-8",
    )
    wrapper = REPO_ROOT / "scripts" / "lib" / "run_with_env.sh"

    result = subprocess.run(
        [str(wrapper), str(project_dir), "env"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "A=John Smith" in result.stdout
    assert "B=x y" in result.stdout
    assert "C=$HOME" in result.stdout
    assert "D=pa$$w" in result.stdout


def test_run_with_env_strips_quotes_and_keeps_quoted_empty(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text(
        "E=\"\"\nF=''\nG=\"a b\"\nH='\"'\n",
        encoding="utf-8",
    )
    wrapper = REPO_ROOT / "scripts" / "lib" / "run_with_env.sh"

    result = subprocess.run(
        [str(wrapper), str(project_dir), "env"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "\nE=\n" in result.stdout
    assert "\nF=\n" in result.stdout
    assert "G=a b" in result.stdout
    assert 'H="' in result.stdout


def test_env_parsers_avoid_negative_substring_length() -> None:
    """``${x:1:-1}`` needs bash 4.2+; macOS ships 3.2 and launchd runs
    ``run_with_env.sh`` with it, so a quoted value would abort the wrapper."""
    for rel in (
        "scripts/lib/run_with_env.sh",
        "scripts/install-launchd-user.sh",
        "scripts/install-systemd-user.sh",
    ):
        assert ":-1}" not in (REPO_ROOT / rel).read_text(encoding="utf-8"), rel


def test_run_with_env_tolerates_export_spacing_and_bad_keys(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text(
        "export  FOO=bar\nexport\tTAB=1\nMY KEY=value\nOK=yes\nexportRAW=1\n",
        encoding="utf-8",
    )
    wrapper = REPO_ROOT / "scripts" / "lib" / "run_with_env.sh"

    result = subprocess.run(
        [str(wrapper), str(project_dir), "env"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "FOO=bar" in result.stdout
    assert "TAB=1" in result.stdout
    assert "OK=yes" in result.stdout
    assert "exportRAW=1" in result.stdout  # python-dotenv keeps this key as-is
    assert "MY KEY" not in result.stdout
    assert "skipping malformed .env line" in result.stderr


# ---------------------------------------------------------------------------
# D) install.sh -- restored vault without .env
# ---------------------------------------------------------------------------


def test_install_sh_handles_restored_vault_without_env(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "vault").mkdir()  # a vault restored from backup, no .env

    shutil.copy(REPO_ROOT / "install.sh", project_dir / "install.sh")
    (project_dir / "install.sh").chmod(0o755)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "uv")
    _write_stub(bin_dir, "jq")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(project_dir / "install.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (project_dir / ".env").exists()
    assert ".env.example" in result.stderr


# ---------------------------------------------------------------------------
# D) scripts/doctor.sh -- uv run needs --directory, same as the installers
# ---------------------------------------------------------------------------


def test_doctor_sh_uses_uv_directory_flag() -> None:
    script = (REPO_ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert '--directory "$PROJECT_DIR"' in script


# ---------------------------------------------------------------------------
# E) Documentation drift
# ---------------------------------------------------------------------------


def test_readmes_document_grok_and_opencode_and_macos() -> None:
    for name in ("README.md", "README.ru.md"):
        content = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "Grok CLI" in content
        assert "opencode" in content
        assert "macos.md" in content


def test_getting_started_documents_grok_and_opencode_and_macos() -> None:
    for language in ("en", "ru"):
        content = (
            REPO_ROOT / "docs" / language / "getting-started.md"
        ).read_text(encoding="utf-8")
        assert "Grok CLI" in content
        assert "opencode" in content
        assert "macos.md" in content


def test_macos_guides_create_the_vault_and_drop_unused_dir() -> None:
    for language in ("en", "ru"):
        content = (REPO_ROOT / "docs" / language / "macos.md").read_text(
            encoding="utf-8"
        )
        assert "a-second-brain init" in content
        assert "second-brain-data" not in content
        assert "up to six" in content or "до шести" in content


def test_development_shellcheck_gate_covers_scripts_lib() -> None:
    for language in ("en", "ru"):
        content = (
            REPO_ROOT / "docs" / language / "development.md"
        ).read_text(encoding="utf-8")
        assert "scripts/lib/*.sh" in content


# ---------------------------------------------------------------------------
# F) doctor.py -- new warnings and case-insensitive key lookup
# ---------------------------------------------------------------------------


def test_doctor_warns_about_symlink_in_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)

    vault_dir = tmp_path / "vault"
    real_dir = tmp_path / "vault_real"
    vault_dir.rename(real_dir)
    vault_dir.symlink_to(real_dir, target_is_directory=True)

    output = io.StringIO()
    doctor.run_doctor(tmp_path, environ=_valid_environment(), stream=output)

    rendered = output.getvalue()
    assert "[WARN]" in rendered
    assert "symlink" in rendered.lower()
    assert str(vault_dir) in rendered


def test_doctor_warns_about_unquoted_space_or_dollar_in_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)

    env_path = tmp_path / ".env"
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\nOWNER_FULL_NAME=John Smith\nSAFE_QUOTED=\"John Smith\"\n"
            "SAFE_COMMENTED=\"John Smith\" # inline comment\n"
            "PLAIN_COMMENTED=abc # trailing note\n"
        )

    output = io.StringIO()
    doctor.run_doctor(tmp_path, environ=_valid_environment(), stream=output)

    rendered = output.getvalue()
    assert "OWNER_FULL_NAME in .env has an unquoted space or '$'" in rendered
    assert "SAFE_QUOTED" not in rendered
    assert "SAFE_COMMENTED" not in rendered
    assert "PLAIN_COMMENTED" not in rendered
    assert "John Smith" not in rendered  # never echo the raw value


def test_doctor_case_insensitive_lookup_fixes_lowercase_known_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "telegram_bot_token=lowercase-secret\n"
        "DEEPGRAM_API_KEY=deepgram-secret\n"
        "OWNER_TELEGRAM_ID=123456\n"
        "AI_CLI=gemini\n"
        "GEMINI_API_KEY=gemini-secret\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)

    output = io.StringIO()
    status = doctor.run_doctor(tmp_path, environ={}, stream=output)

    rendered = output.getvalue()
    # The lowercase key still satisfies the check (case-insensitive lookup)...
    assert "[OK] TELEGRAM_BOT_TOKEN is set" in rendered
    assert status == 0
    # ...but doctor flags the non-canonical casing.
    assert (
        ".env key 'telegram_bot_token' should be uppercase 'TELEGRAM_BOT_TOKEN'"
        in rendered
    )
    assert "lowercase-secret" not in rendered


def test_doctor_warns_about_invalid_plaud_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    environment = {**_valid_environment(), "PLAUD_REGION": "eu-west-1"}

    output = io.StringIO()
    doctor.run_doctor(tmp_path, environ=environment, stream=output)

    rendered = output.getvalue()
    assert "PLAUD_REGION 'eu-west-1' is not one of: api, api-euc1" in rendered


def test_doctor_accepts_valid_plaud_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    environment = {**_valid_environment(), "PLAUD_REGION": "api-euc1"}

    output = io.StringIO()
    doctor.run_doctor(tmp_path, environ=environment, stream=output)

    assert "PLAUD_REGION" not in output.getvalue()
