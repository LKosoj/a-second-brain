import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from d_brain import run_compiled_reprocess_days
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


def test_main_updated_notes_counter_ignores_duplicate_skip_days(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Defect 1 (Resolve code review): ``refresh_daily_fully`` already
    excludes duplicate-chunk skips from its ``updated`` list (see
    CompiledBriefingService.refresh_after_write); this locks in that the
    manual-reprocess report/progress counters built on top of it
    (``updated_notes``, ``updated_total``, ``had_updates``) stay at zero
    when every reprocessed day was a no-op skip, instead of an operator
    seeing "updated" for a rerun that changed nothing."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2025-06-01.md").write_text("# Day 1\n", encoding="utf-8")
    (vault / "daily" / "2025-06-02.md").write_text("# Day 2\n", encoding="utf-8")

    monkeypatch.setattr(
        run_compiled_reprocess_days,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault,
            content_language="ru",
            ai_cli="claude",
        ),
    )

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def refresh_daily_fully(self, *, source_path, refresh_qmd, on_chunk):  # type: ignore[no-untyped-def]
            del refresh_qmd, on_chunk
            # Every chunk of every day is a duplicate-chunk skip: no updates,
            # no errors.
            return {
                "available": True,
                "updated": [],
                "errors": [],
                "chunks": 1,
                "processed_chunks": 1,
                "source_rel_path": source_path,
            }

        def _refresh_qmd_index(self) -> None:
            pass

    monkeypatch.setattr(
        run_compiled_reprocess_days,
        "CompiledBriefingService",
        FakeService,
    )
    monkeypatch.setattr(
        sys, "argv", ["prog", "2025-06-01.md", "2025-06-02.md"]
    )

    exit_code = run_compiled_reprocess_days.main()

    assert exit_code == 0
    progress = json.loads(
        (vault / ".compiled" / "manual-reprocess-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["updated_notes"] == 0
    assert progress["finished"] is True

    report_files = [
        path
        for path in (vault / ".compiled").glob("manual-reprocess-*.json")
        if path.name != "manual-reprocess-progress.json"
    ]
    assert len(report_files) == 1
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert report["updated_total"] == []
    assert report["had_updates"] is False


def test_the_same_page_updated_on_two_days_is_counted_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The counter is a count of *pages*, not of day-page pairs. Reprocessing
    a week normally touches the same compiled page again and again, so
    without the cross-day dedupe (``if item not in updated_total``) the
    operator's "updated" number silently inflates with every extra day. The
    all-skips test above cannot see this: with nothing ever updated, the
    dedupe branch never runs at all."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2025-06-01.md").write_text("# Day 1\n", encoding="utf-8")
    (vault / "daily" / "2025-06-02.md").write_text("# Day 2\n", encoding="utf-8")

    monkeypatch.setattr(
        run_compiled_reprocess_days,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault,
            content_language="ru",
            ai_cli="claude",
        ),
    )

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def refresh_daily_fully(self, *, source_path, refresh_qmd, on_chunk):  # type: ignore[no-untyped-def]
            del refresh_qmd, on_chunk
            # Both days feed the same compiled page, and the second day also
            # lists it twice within its own result.
            return {
                "available": True,
                "updated": [
                    "compiled/topics/aurora.md",
                    "compiled/topics/aurora.md",
                ],
                "errors": [],
                "chunks": 1,
                "processed_chunks": 1,
                "source_rel_path": source_path,
            }

        def _refresh_qmd_index(self) -> None:
            pass

    monkeypatch.setattr(
        run_compiled_reprocess_days,
        "CompiledBriefingService",
        FakeService,
    )
    monkeypatch.setattr(sys, "argv", ["prog", "2025-06-01.md", "2025-06-02.md"])

    exit_code = run_compiled_reprocess_days.main()

    assert exit_code == 0
    progress = json.loads(
        (vault / ".compiled" / "manual-reprocess-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["updated_notes"] == 1

    report_files = [
        path
        for path in (vault / ".compiled").glob("manual-reprocess-*.json")
        if path.name != "manual-reprocess-progress.json"
    ]
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert report["updated_total"] == ["compiled/topics/aurora.md"]
    assert report["had_updates"] is True
    # Each day still reports its own updates, deduped within the day.
    assert [entry["updated"] for entry in report["days"]] == [
        ["compiled/topics/aurora.md"],
        ["compiled/topics/aurora.md"],
    ]
