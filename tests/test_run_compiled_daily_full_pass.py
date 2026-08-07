import json
import sys
from pathlib import Path
from types import SimpleNamespace

from d_brain import run_compiled_daily_full_pass


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_daily_full_pass,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault,
            content_language="ru",
            ai_cli="claude",
        ),
    )


def test_main_stops_the_whole_pass_on_a_terminal_backend_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A quota/rate-limit refusal is terminal: every remaining day would fail
    the same way, each one burning a model call and writing an empty history
    entry that reads like a real, finished day. The run must stop on the
    first one, exit non-zero, and leave ``finished`` false with the reason
    recorded, so a resume picks up exactly where it stopped."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2025-04-30.md").write_text("# Day 1\n", encoding="utf-8")
    (vault / "daily" / "2025-05-01.md").write_text("# Day 2\n", encoding="utf-8")
    _patch_settings(monkeypatch, vault)

    seen: list[str] = []

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def refresh_daily_fully(self, *, source_path, refresh_qmd, on_chunk):  # type: ignore[no-untyped-def]
            del refresh_qmd, on_chunk
            seen.append(source_path)
            return {
                "available": True,
                "updated": [],
                "errors": ["compiled briefing render: quota exceeded"],
                "chunks": 1,
                "processed_chunks": 1,
                "source_rel_path": source_path,
            }

        def _refresh_qmd_index(self) -> None:
            raise AssertionError("qmd must not be refreshed after a terminal stop")

    monkeypatch.setattr(
        run_compiled_daily_full_pass, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--start-from", "2025-04-30.md"])

    exit_code = run_compiled_daily_full_pass.main()

    assert exit_code == 1
    assert seen == ["daily/2025-04-30.md"]  # the second day was never started
    progress = json.loads(
        (vault / ".compiled" / "daily-full-pass-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["stopped_reason"] == "terminal-backend-error"
    assert progress["finished"] is False
    assert progress["next_file"] == "2025-05-01.md"


def test_main_resume_continues_from_the_progress_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """``--resume`` must start from the progress file's own ``current_file``,
    not from ``--start-from``'s default: after a terminal stop the operator
    reruns the same command line, and re-processing days that already
    succeeded would spend the quota that stopped the run in the first
    place."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    for name in ("2025-04-30.md", "2025-05-01.md", "2025-05-02.md"):
        (vault / "daily" / name).write_text(f"# {name}\n", encoding="utf-8")
    progress_path = vault / ".compiled" / "daily-full-pass-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps({"current_file": "2025-05-02.md", "next_file": None}),
        encoding="utf-8",
    )
    _patch_settings(monkeypatch, vault)

    seen: list[str] = []

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def refresh_daily_fully(self, *, source_path, refresh_qmd, on_chunk):  # type: ignore[no-untyped-def]
            del refresh_qmd, on_chunk
            seen.append(source_path)
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
        run_compiled_daily_full_pass, "CompiledBriefingService", FakeService
    )
    monkeypatch.setattr(
        sys, "argv", ["prog", "--resume", "--start-from", "2025-04-30.md"]
    )

    exit_code = run_compiled_daily_full_pass.main()

    assert exit_code == 0
    assert seen == ["daily/2025-05-02.md"]


def test_main_updated_notes_counter_ignores_duplicate_skip_days(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Defect 1 (Resolve code review): ``refresh_daily_fully`` already
    excludes duplicate-chunk skips from its ``updated`` list (see
    CompiledBriefingService.refresh_after_write); this locks in that the
    historical full-pass progress/history counter (``updated_notes``) built
    on top of it stays at zero when every processed day was a no-op skip,
    instead of an operator seeing "updated" for a rerun that changed
    nothing."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2025-04-30.md").write_text("# Day 1\n", encoding="utf-8")
    (vault / "daily" / "2025-05-01.md").write_text("# Day 2\n", encoding="utf-8")

    monkeypatch.setattr(
        run_compiled_daily_full_pass,
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
            # Every chunk of every day is a duplicate-chunk skip: no
            # updates, no errors.
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
        run_compiled_daily_full_pass,
        "CompiledBriefingService",
        FakeService,
    )
    monkeypatch.setattr(
        sys, "argv", ["prog", "--start-from", "2025-04-30.md"]
    )

    exit_code = run_compiled_daily_full_pass.main()

    assert exit_code == 0
    progress = json.loads(
        (vault / ".compiled" / "daily-full-pass-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["updated_notes"] == 0
    assert progress["finished"] is True

    history_lines = (
        (vault / ".compiled" / "daily-full-pass-history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(history_lines) == 2
    for line in history_lines:
        entry = json.loads(line)
        assert entry["updated"] == []
