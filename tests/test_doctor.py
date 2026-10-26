from __future__ import annotations

import io
import stat
import subprocess
from datetime import timedelta
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


def test_doctor_checks_codex_binary_and_tmux_for_codex_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "AI_CLI": "codex-tmux"}
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda command: f"/usr/bin/{command}"
        if command in {"uv", "jq", "codex"}
        else None,
    )
    output = io.StringIO()

    status = doctor.run_doctor(tmp_path, environ=environment, stream=output)

    rendered = output.getvalue()
    assert status == 1
    assert "[OK] AI_CLI selects 'codex-tmux'" in rendered
    assert "[OK] AI CLI 'codex-tmux' is installed" in rendered
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


def _doctor_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    **kwargs: object,
) -> tuple[int, str]:
    monkeypatch.setattr(doctor.shutil, "which", _installed_core_command)
    output = io.StringIO()
    status = doctor.run_doctor(
        tmp_path, environ=environment, stream=output, **kwargs  # type: ignore[arg-type]
    )
    return status, output.getvalue()


def test_doctor_warns_when_deepgram_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = _valid_environment()
    del environment["DEEPGRAM_API_KEY"]

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 0
    assert "Result: PASS" in rendered
    assert "[WARN] DEEPGRAM_API_KEY" in rendered


def test_doctor_telegram_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize_project(tmp_path)

    status, rendered = _doctor_output(
        tmp_path,
        monkeypatch,
        _valid_environment(),
        telegram_probe=lambda token: ("ok", "@d_brain_bot"),
    )

    assert status == 0
    assert "[OK] Telegram accepted TELEGRAM_BOT_TOKEN (@d_brain_bot)" in rendered
    assert "telegram-secret" not in rendered


def test_doctor_telegram_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)

    status, rendered = _doctor_output(
        tmp_path,
        monkeypatch,
        _valid_environment(),
        telegram_probe=lambda token: ("rejected", ""),
    )

    assert status == 1
    assert "[ERR] Telegram rejected TELEGRAM_BOT_TOKEN" in rendered
    assert "Result: FAIL" in rendered


def test_doctor_telegram_unreachable_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)

    status, rendered = _doctor_output(
        tmp_path,
        monkeypatch,
        _valid_environment(),
        telegram_probe=lambda token: ("unreachable", "ConnectError"),
    )

    assert status == 0
    assert "[WARN] Could not reach Telegram" in rendered
    assert "ConnectError" in rendered


def test_doctor_skips_telegram_probe_when_environ_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("doctor must not call the network here")

    monkeypatch.setattr(doctor.httpx, "get", no_network)

    status, rendered = _doctor_output(tmp_path, monkeypatch, _valid_environment())

    assert status == 0
    assert "Telegram" not in rendered.replace("TELEGRAM_BOT_TOKEN", "")


def test_doctor_real_probe_unreachable_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(url: str, **kwargs: object) -> None:
        raise doctor.httpx.ConnectError(f"{url} unreachable")

    monkeypatch.setattr(doctor.httpx, "get", fail)

    outcome, detail = doctor._probe_telegram("secret-token")

    assert outcome == "unreachable"
    assert "secret-token" not in detail


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"ok": True, "result": {"username": "d_brain_bot"}}, "ok"),
        (401, {"ok": False}, "rejected"),
        (404, {"ok": False}, "rejected"),
        (429, {"ok": False}, "unreachable"),
        (502, {}, "unreachable"),
    ],
)
def test_doctor_real_probe_maps_responses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    payload: dict[str, object],
    expected: str,
) -> None:
    def respond(url: str, **kwargs: object) -> doctor.httpx.Response:
        return doctor.httpx.Response(status_code, json=payload)

    monkeypatch.setattr(doctor.httpx, "get", respond)

    assert doctor._probe_telegram("secret-token")[0] == expected


def test_doctor_warns_about_invalid_schedule_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {
        **_valid_environment(),
        "DAILY_PROCESS_TIME": "25:00",
        "MORNING_BRIEF_TIME": "08:00",
    }

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 0
    assert "[WARN] DAILY_PROCESS_TIME must be HH:MM" in rendered
    assert "MORNING_BRIEF_TIME must" not in rendered


def test_doctor_warns_about_small_hours_process_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "DAILY_PROCESS_TIME": "03:30"}

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 0
    assert "[WARN] DAILY_PROCESS_TIME 03:30" in rendered


def test_doctor_accepts_evening_process_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "DAILY_PROCESS_TIME": "21:00"}

    _, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert "DAILY_PROCESS_TIME 21:00" not in rendered


def test_doctor_probe_invalid_url_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(url: str, **kwargs: object) -> None:
        raise doctor.httpx.InvalidURL(f"bad url {url}")

    monkeypatch.setattr(doctor.httpx, "get", fail)

    outcome, detail = doctor._probe_telegram("secret-token\n")

    assert outcome == "unreachable"
    assert detail == "InvalidURL"


def test_doctor_warns_about_legacy_openai_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {
        **_valid_environment(),
        "BASE_URL": "https://secret-gateway.example/v1",
        "MODEL": "secret-model",
    }

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 0
    assert "BASE_URL is the legacy name; rename it to OPENAI_BASE_URL" in rendered
    assert "MODEL is the legacy name; rename it to OPENAI_MODEL" in rendered
    assert "secret-gateway" not in rendered
    assert "secret-model" not in rendered


def test_doctor_warns_when_both_openai_names_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {
        **_valid_environment(),
        "BASE_URL": "https://old.example/v1",
        "OPENAI_BASE_URL": "https://new.example/v1",
        "MODEL": "same",
        "OPENAI_MODEL": "same",
    }

    _status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert "both BASE_URL and OPENAI_BASE_URL are set; OPENAI_BASE_URL wins" in rendered
    assert "both MODEL and OPENAI_MODEL" not in rendered


def test_doctor_timezone_unset_reports_system_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor, "system_timezone_name", lambda: "Europe/Moscow")

    _, rendered = _doctor_output(tmp_path, monkeypatch, _valid_environment())

    assert "[OK] TIMEZONE is not set; the system zone Europe/Moscow" in rendered


def test_doctor_rejects_unknown_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    environment = {**_valid_environment(), "TIMEZONE": "Mars/Base"}

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 1
    assert "[ERR] TIMEZONE must be an IANA name" in rendered


def test_doctor_warns_when_timezone_differs_from_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor, "system_timezone_name", lambda: "UTC")
    monkeypatch.setattr(doctor, "_system_utc_offset", lambda: timedelta(0))
    environment = {**_valid_environment(), "TIMEZONE": "Asia/Tokyo"}

    status, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert status == 0
    assert "[WARN] TIMEZONE Asia/Tokyo differs from the system zone UTC" in rendered
    assert "launchd" in rendered
    assert "daylight" in rendered


def test_doctor_accepts_timezone_matching_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_project(tmp_path)
    monkeypatch.setattr(doctor, "system_timezone_name", lambda: "Etc/UTC")
    monkeypatch.setattr(doctor, "_system_utc_offset", lambda: timedelta(0))
    environment = {**_valid_environment(), "TIMEZONE": "UTC"}

    _, rendered = _doctor_output(tmp_path, monkeypatch, environment)

    assert "[OK] TIMEZONE UTC" in rendered
    assert "differs from the system zone" not in rendered
