import subprocess
from pathlib import Path

from d_brain.run_compiled_reprocess_days import _spawn_full_pass


def test_spawn_full_pass_prefers_systemd_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    project_root = vault.parent
    python_bin = (project_root / ".venv" / "bin" / "python").resolve()
    log_path = tmp_path / "resume.log"

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        calls.append(command)
        if command[0] == "systemd-run":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["systemctl", "show", calls[0][4].split("=", 1)[1]]:
            return subprocess.CompletedProcess(command, 0, stdout="4321\n", stderr="")
        raise AssertionError(command)

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        "d_brain.run_compiled_reprocess_days.shutil.which",
        lambda name: "/usr/bin/systemd-run",
    )
    monkeypatch.setattr(
        "d_brain.run_compiled_reprocess_days.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "d_brain.run_compiled_reprocess_days.subprocess.Popen",
        fake_popen,
    )

    pid, unit = _spawn_full_pass(
        vault,
        start_from="2025-12-06.md",
        log_path=log_path,
    )

    assert pid == 4321
    assert unit is not None
    assert unit.startswith("dbrain-compiled-full-pass-")
    assert calls[0][0] == "systemd-run"
    assert f"--working-directory={project_root}" in calls[0]
    assert calls[0][-2:] == [
        "-lc",
        (
            f"cd {project_root} && "
            f"exec {python_bin} -m d_brain.run_compiled_daily_full_pass "
            "--resume --start-from 2025-12-06.md "
            f">> {log_path} 2>&1"
        ),
    ]


def test_spawn_full_pass_falls_back_to_detached_python_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    log_path = tmp_path / "resume.log"
    project_root = vault.parent
    python_bin = str((project_root / ".venv" / "bin" / "python").resolve())
    spawned: dict[str, object] = {}

    class FakeProcess:
        pid = 6789

    def fake_popen(command, **kwargs):  # type: ignore[no-untyped-def]
        spawned["command"] = command
        spawned["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "d_brain.run_compiled_reprocess_days.shutil.which",
        lambda name: None,
    )
    monkeypatch.setattr(
        "d_brain.run_compiled_reprocess_days.subprocess.Popen",
        fake_popen,
    )

    pid, unit = _spawn_full_pass(
        vault,
        start_from="2025-12-06.md",
        log_path=log_path,
    )

    assert pid == 6789
    assert unit is None
    assert spawned["command"] == [
        python_bin,
        "-m",
        "d_brain.run_compiled_daily_full_pass",
        "--resume",
        "--start-from",
        "2025-12-06.md",
    ]
    kwargs = spawned["kwargs"]
    assert kwargs["cwd"] == project_root
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
