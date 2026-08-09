from __future__ import annotations

import io
import stat
import subprocess
from pathlib import Path

import pytest

from d_brain import doctor
from d_brain.cli import initialize_project, main


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


def test_doctor_passes_without_printing_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    rendered = output.getvalue()
    assert status == 0
    assert "Result: PASS" in rendered
    assert "telegram-secret" not in rendered
    assert "deepgram-secret" not in rendered
    assert "gemini-secret" not in rendered


def test_doctor_accepts_kimi_and_shows_login_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "AI_CLI": "kimi"}
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda command: f"/usr/bin/{command}"
        if command in {"uv", "jq", "kimi"}
        else None,
    )
    output = io.StringIO()

    status = doctor.run_doctor(tmp_path, environ=environment, stream=output)

    assert status == 0
    assert "[OK] AI_CLI selects 'kimi'" in output.getvalue()
    assert "run: kimi login" in output.getvalue()


def test_doctor_checks_claude_binary_and_tmux_for_claude_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "AI_CLI": "claude-tmux"}
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda command: f"/usr/bin/{command}"
        if command in {"uv", "jq", "claude"}
        else None,
    )
    output = io.StringIO()

    status = doctor.run_doctor(tmp_path, environ=environment, stream=output)

    rendered = output.getvalue()
    assert status == 1
    assert "[OK] AI_CLI selects 'claude-tmux'" in rendered
    assert "[OK] AI CLI 'claude-tmux' is installed" in rendered
    assert "requires tmux, which is missing" in rendered


def test_doctor_fails_when_env_file_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    (tmp_path / ".env").unlink()
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 1
    assert f"[ERR] .env is missing at {tmp_path / '.env'}" in output.getvalue()


def test_doctor_rejects_invalid_owner_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    environment = {**_valid_environment(), "OWNER_TELEGRAM_ID": "0"}
    output = io.StringIO()

    status = doctor.run_doctor(tmp_path, environ=environment, stream=output)

    assert status == 1
    assert "[ERR] OWNER_TELEGRAM_ID must be a positive integer" in output.getvalue()


def test_doctor_reports_valid_control_plane_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor's own wiring/formatting for a clean registry result, not the
    registry validator itself (see test_prompt_corpus.py for that) — the
    validator is stubbed so this test and the validator test do not fail
    in lockstep from the same underlying registry defect."""
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    monkeypatch.setattr(doctor, "validate_control_plane_registry", lambda: [])
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 0
    assert (
        "[OK] Control-plane workflow registry is valid" in output.getvalue()
    )


def test_doctor_fails_when_control_plane_registry_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    monkeypatch.setattr(
        doctor, "validate_control_plane_registry", lambda: ["broken thing"]
    )
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 1
    assert (
        "[ERR] Control-plane registry: broken thing" in output.getvalue()
    )


def test_doctor_reports_each_control_plane_registry_error_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    monkeypatch.setattr(
        doctor,
        "validate_control_plane_registry",
        lambda: ["first problem", "second problem"],
    )
    output = io.StringIO()

    doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    rendered = output.getvalue()
    assert "[ERR] Control-plane registry: first problem" in rendered
    assert "[ERR] Control-plane registry: second problem" in rendered


def test_doctor_survives_unexpected_registry_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_control_plane_registry() only promises to catch
    (AttributeError, ImportError) internally; any other exception raised
    while importing a workflow module (e.g. a settings validation error
    that fires at import time) must not crash doctor -- it should surface
    as a diagnostic ERR instead, same as every other _check_* method."""
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)

    def _explode() -> list[str]:
        raise RuntimeError("settings validation failed for workflow module")

    monkeypatch.setattr(doctor, "validate_control_plane_registry", _explode)
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 1
    assert (
        "[ERR] Control-plane registry: settings validation failed for workflow module"
        in output.getvalue()
    )


def test_doctor_requires_todoist_commands_only_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    environment = {**_valid_environment(), "TODOIST_API_KEY": "todoist-secret"}
    output = io.StringIO()

    status = doctor.run_doctor(tmp_path, environ=environment, stream=output)

    assert status == 1
    assert "required commands are missing: mcp-cli, npx" in output.getvalue()
    assert "todoist-secret" not in output.getvalue()


def test_doctor_warns_about_permissive_env_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    (tmp_path / ".env").chmod(0o644)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 0
    assert "[WARN] .env permissions are 644" in output.getvalue()
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o644


def test_doctor_smoke_uses_current_python_without_exposing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="OK\n", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    output = io.StringIO()

    status = doctor.run_doctor(
        tmp_path,
        smoke=True,
        environ=_valid_environment(),
        stream=output,
    )

    assert status == 0
    assert calls[0][1:4] == ["-m", "d_brain.run_agent", "--ai-cli"]
    assert "[OK] AI CLI smoke test passed" in output.getvalue()


def test_cli_routes_doctor_target_and_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, bool]] = []

    def fake_doctor(target: Path, *, smoke: bool = False) -> int:
        calls.append((target, smoke))
        return 7

    monkeypatch.setattr(doctor, "run_doctor", fake_doctor)

    assert main(["doctor", str(tmp_path), "--smoke"]) == 7
    assert calls == [(tmp_path, True)]


def test_shell_doctor_is_a_thin_cli_wrapper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "scripts/doctor.sh").read_text(encoding="utf-8")

    assert "a-second-brain doctor" in script
    assert "TELEGRAM_BOT_TOKEN" not in script
    assert "OWNER_TELEGRAM_ID" not in script
