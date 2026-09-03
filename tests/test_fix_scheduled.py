"""Tests for audit items 1, 12 and 19: restricted scheduled-cycle execution,
self-disabling periodic steps (job health), and daily idempotency + VERIFY.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import _setup_daily_processing_vault, _write_vault_manifest

from d_brain.bot.dashboard import (
    build_disabled_steps_keyboard,
    build_disabled_steps_text,
    reenable_job_step,
)
from d_brain.services.cli_runner import CliRunner
from d_brain.services.entry_status import ENTRY_STATUS_ALREADY_PROCESSED
from d_brain.services.processor import (
    INTERACTIVE_MODE,
    JOB_HEALTH_DISABLE_THRESHOLD,
    REFLECT_DAILY_START_MARKER,
    SCHEDULED_MODE,
    CliProcessor,
    cycle_step_label,
    load_job_health,
    save_job_health,
)


@pytest.fixture(autouse=True)
def _block_real_ai_cli() -> None:
    """Override conftest's autouse block: one test below exercises
    ``CliRunner.run`` itself with a fake subprocess (same override as
    ``tests/test_cli_runner.py``). Every other test in this module replaces
    ``_run_vault_prompt``/``_run_prompt``/the per-instance runners directly,
    so it never reaches ``CliRunner.run`` regardless of this override.
    """


def _fake_popen(calls: list[dict[str, object]], *, stdout: str):
    """Minimal ``subprocess.Popen`` replacement recording argv (see
    ``tests/test_cli_runner.py``'s helper of the same name, not exported)."""

    class _FakeProcess:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            calls.append({"args": args, "kwargs": kwargs})
            self.returncode = 0

        def __enter__(self) -> "_FakeProcess":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            del timeout
            calls[-1]["input"] = input
            return stdout, ""

    return _FakeProcess


# ---------------------------------------------------------------------------
# Task 1: restricted (no shell / no network) execution for the nightly cycle
# ---------------------------------------------------------------------------


def test_build_command_restricted_adds_disallowed_tools_for_claude() -> None:
    runner = CliRunner(Path("."), "claude")

    interactive = runner.build_command("hello")
    restricted = runner.build_command("hello", restricted=True)

    assert "--disallowedTools" not in interactive
    assert "--disallowedTools" in restricted
    assert "Bash,WebFetch,WebSearch" in restricted


def test_build_command_restricted_swaps_sandbox_flag_for_codex() -> None:
    runner = CliRunner(Path("."), "codex")

    interactive = runner.build_command("hello")
    restricted = runner.build_command("hello", restricted=True)

    assert "--dangerously-bypass-approvals-and-sandbox" in interactive
    assert "--dangerously-bypass-approvals-and-sandbox" not in restricted
    assert "--sandbox" in restricted
    assert "workspace-write" in restricted


def test_build_command_restricted_is_noop_for_backend_without_a_flag() -> None:
    # gemini/qwen/kimi/grok/opencode have no known deny-tools flag (see
    # cli_runner.py's CliSpec comments); restricted must not invent one.
    runner = CliRunner(Path("."), "gemini")

    assert runner.build_command("hi", restricted=True) == runner.build_command("hi")


def test_build_command_restricted_warns_once_for_backend_without_a_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # "restricted" without a real deny-tools flag must not fail silently --
    # a warning tells whoever reads the service log that the nightly cycle
    # ran this backend unrestricted (see cli_runner.py's CliSpec comments
    # for which backends do/don't support restricted mode).
    runner = CliRunner(Path("."), "gemini")
    caplog.set_level("WARNING", logger="d_brain.services.cli_runner")

    runner.build_command("hi", restricted=True)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "gemini" in warnings[0].getMessage()
    assert "restricted" in warnings[0].getMessage().lower()

    caplog.clear()
    # Interactive (unrestricted) calls must stay silent.
    runner.build_command("hi", restricted=False)
    assert not caplog.records


def test_run_uses_restricted_stdin_prefix_for_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claude always executes over stdin (CliSpec.stdin_prefix), never through
    # build_command's argv path -- restriction must be threaded into that
    # stdin path too, not just build_command.
    runner = CliRunner(Path("."), "claude")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "d_brain.services.cli_runner.subprocess.Popen",
        _fake_popen(calls, stdout='{"type":"result","result":"ok"}\n'),
    )

    runner.run("hello", timeout=10, restricted=True)

    assert calls[0]["args"] == (list(runner.spec.restricted_stdin_prefix),)


def test_run_prompt_and_run_vault_prompt_thread_restricted_mode(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)
    seen: list[bool] = []

    def fake_run(prompt, *, timeout, extra_env=None, restricted=False):  # type: ignore[no-untyped-def]
        del prompt, timeout, extra_env
        seen.append(restricted)
        return "ok"

    processor._project_runner.run = fake_run  # type: ignore[method-assign]
    processor._assistant_runner.run = fake_run  # type: ignore[method-assign]

    processor._run_prompt("hi")
    processor._run_vault_prompt("hi")
    assert seen == [False, False]

    processor._restricted_mode = True
    processor._run_prompt("hi")
    processor._run_vault_prompt("hi")
    assert seen == [False, False, True, True]


def test_run_scheduled_cycle_is_restricted_but_interactive_process_is_not(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 6)  # Monday: no periodic reviews, no goals rollover
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)

    restricted_seen_daily: list[bool] = []

    def fake_process_daily(target_day, mode=SCHEDULED_MODE):  # type: ignore[no-untyped-def]
        del target_day
        restricted_seen_daily.append((mode, processor._restricted_mode))
        return {"report": "📊 **Daily**", "processed_entries": 0}

    processor.process_daily = fake_process_daily  # type: ignore[method-assign]
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }
    processor.audit_cycle_result = lambda **kwargs: {  # type: ignore[method-assign]
        "cycle_name": kwargs["cycle_name"],
        "label": kwargs["cycle_name"],
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    processor.run_scheduled_cycle(day)

    assert restricted_seen_daily == [(SCHEDULED_MODE, True)]
    # Cleared once the cycle finishes so an unrelated later call is unaffected.
    assert processor._restricted_mode is False

    # A direct interactive call never toggles restricted mode at all.
    restricted_seen_daily.clear()
    processor.process_daily = fake_process_daily  # type: ignore[method-assign]
    processor.process_daily(day, mode=INTERACTIVE_MODE)
    assert restricted_seen_daily == [(INTERACTIVE_MODE, False)]


# ---------------------------------------------------------------------------
# Task 12: periodic steps self-disable after 3 consecutive failures
# ---------------------------------------------------------------------------


def _run_minimal_scheduled_cycle(
    processor: CliProcessor,
    day: date,
    *,
    weekly_digest_result,  # type: ignore[no-untyped-def]
):
    """Drive ``run_scheduled_cycle`` with everything except weekly_digest
    stubbed to a harmless success, so only that one step's health matters."""
    processor.process_daily = lambda d, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 0,
    }
    processor.generate_weekly_digest = lambda **kwargs: weekly_digest_result  # type: ignore[method-assign]
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }
    processor.audit_cycle_result = lambda **kwargs: {  # type: ignore[method-assign]
        "cycle_name": kwargs["cycle_name"],
        "label": kwargs["cycle_name"],
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    return processor.run_scheduled_cycle(day)


def test_periodic_step_disables_after_three_consecutive_failures_and_is_skipped(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 3)  # Friday: weekly_digest is due
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    failing_result = {"error": "boom", "processed_entries": 0}

    for attempt in range(1, JOB_HEALTH_DISABLE_THRESHOLD + 1):
        result = _run_minimal_scheduled_cycle(
            processor, day, weekly_digest_result=failing_result
        )
        health = load_job_health(vault_path)
        assert health["weekly_digest"]["consecutive_failures"] == attempt
        if attempt < JOB_HEALTH_DISABLE_THRESHOLD:
            assert health["weekly_digest"]["disabled"] is False
        else:
            assert health["weekly_digest"]["disabled"] is True
            assert "отключён после" in result["report"]

    call_count_before = 0

    def fail_if_called(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count_before
        call_count_before += 1
        return failing_result

    # Not the shared helper: it always reassigns generate_weekly_digest,
    # which would silently replace this spy before the cycle runs.
    processor.process_daily = lambda d, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 0,
    }
    processor.generate_weekly_digest = fail_if_called  # type: ignore[method-assign]
    result = processor.run_scheduled_cycle(day)

    assert call_count_before == 0, "disabled step must not run again"
    assert "шаг" in result["report"].lower()
    assert "отключён" in result["report"]
    health = load_job_health(vault_path)
    assert (
        health["weekly_digest"]["consecutive_failures"] == JOB_HEALTH_DISABLE_THRESHOLD
    )


def test_periodic_step_success_resets_the_failure_streak(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 3)  # Friday
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    failing_result = {"error": "boom", "processed_entries": 0}
    success_result = {
        "report": "📅 **Weekly**",
        "processed_entries": 1,
        "searchable_write": True,
    }

    _run_minimal_scheduled_cycle(processor, day, weekly_digest_result=failing_result)
    _run_minimal_scheduled_cycle(processor, day, weekly_digest_result=failing_result)
    health = load_job_health(vault_path)
    assert health["weekly_digest"]["consecutive_failures"] == 2

    _run_minimal_scheduled_cycle(processor, day, weekly_digest_result=success_result)
    health = load_job_health(vault_path)
    assert health["weekly_digest"]["consecutive_failures"] == 0
    assert health["weekly_digest"]["disabled"] is False

    # Two more failures after the reset must not disable it (needs 3 in a row).
    _run_minimal_scheduled_cycle(processor, day, weekly_digest_result=failing_result)
    _run_minimal_scheduled_cycle(processor, day, weekly_digest_result=failing_result)
    health = load_job_health(vault_path)
    assert health["weekly_digest"]["consecutive_failures"] == 2
    assert health["weekly_digest"]["disabled"] is False


def test_reenable_job_step_clears_disabled_flag_and_counter(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    save_job_health(
        vault_path,
        {
            "weekly_digest": {
                "consecutive_failures": 3,
                "last_error": "boom",
                "last_failure_at": "2026-04-03T21:00:00+00:00",
                "disabled": True,
            }
        },
    )

    was_disabled = reenable_job_step(vault_path, "weekly_digest")

    assert was_disabled is True
    health = load_job_health(vault_path)
    assert health["weekly_digest"]["disabled"] is False
    assert health["weekly_digest"]["consecutive_failures"] == 0
    # Re-enabling something that is not disabled is a harmless no-op.
    assert reenable_job_step(vault_path, "weekly_digest") is False
    assert reenable_job_step(vault_path, "no_such_step") is False


def test_reenable_job_step_leaves_other_entries_untouched_and_file_valid(
    tmp_path: Path,
) -> None:
    # Guards the fix for the read-modify-write race between the nightly
    # cycle and this bot-triggered re-enable: the file on disk must stay
    # valid JSON, and only the targeted step's entry may change.
    vault_path = tmp_path / "vault"
    untouched_entry = {
        "consecutive_failures": 1,
        "last_error": "unrelated",
        "last_failure_at": "2026-04-01T09:00:00+00:00",
        "disabled": False,
    }
    save_job_health(
        vault_path,
        {
            "weekly_digest": {
                "consecutive_failures": 3,
                "last_error": "boom",
                "last_failure_at": "2026-04-03T21:00:00+00:00",
                "disabled": True,
            },
            "monthly": untouched_entry,
        },
    )

    assert reenable_job_step(vault_path, "weekly_digest") is True

    raw = (vault_path / ".session" / "job-health.json").read_text(encoding="utf-8")
    health = json.loads(raw)  # must parse cleanly, never a half-written file
    assert health["monthly"] == untouched_entry
    assert health["weekly_digest"]["disabled"] is False
    assert health["weekly_digest"]["consecutive_failures"] == 0
    assert health["weekly_digest"]["last_error"] == "boom"


def test_reenable_job_step_and_record_job_step_health_hold_vault_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The coordinator flagged a read-modify-write race on job-health.json
    # between the nightly cycle (_record_job_step_health) and this
    # bot-triggered re-enable; both must serialize through vault_write_lock.
    from contextlib import contextmanager

    import d_brain.bot.dashboard as dashboard_module
    import d_brain.services.processor as processor_module

    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    save_job_health(
        vault_path,
        {
            "weekly_digest": {
                "consecutive_failures": 3,
                "last_error": "boom",
                "last_failure_at": "2026-04-03T21:00:00+00:00",
                "disabled": True,
            }
        },
    )

    dashboard_lock_calls: list[Path] = []
    real_dashboard_lock = dashboard_module.vault_write_lock

    @contextmanager
    def spy_dashboard_lock(path):  # type: ignore[no-untyped-def]
        dashboard_lock_calls.append(Path(path))
        with real_dashboard_lock(path) as lock:
            yield lock

    monkeypatch.setattr(dashboard_module, "vault_write_lock", spy_dashboard_lock)
    reenable_job_step(vault_path, "weekly_digest")
    assert dashboard_lock_calls == [vault_path]

    processor_lock_calls: list[Path] = []
    real_processor_lock = processor_module.vault_write_lock

    @contextmanager
    def spy_processor_lock(path):  # type: ignore[no-untyped-def]
        processor_lock_calls.append(Path(path))
        with real_processor_lock(path) as lock:
            yield lock

    monkeypatch.setattr(processor_module, "vault_write_lock", spy_processor_lock)
    processor = CliProcessor(vault_path)
    processor._record_job_step_health("weekly_digest", {"processed_entries": 1})
    assert processor_lock_calls == [vault_path]


def test_disabled_steps_screen_lists_step_and_offers_reenable_button(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    save_job_health(
        vault_path,
        {
            "weekly_digest": {
                "consecutive_failures": 3,
                "last_error": "boom",
                "last_failure_at": "2026-04-03T21:00:00+00:00",
                "disabled": True,
            }
        },
    )

    text = build_disabled_steps_text(vault_path)
    # Russian label from the same dictionary CliProcessor._cycle_label uses,
    # not the raw step-name key -- the owner shouldn't see "weekly_digest".
    assert cycle_step_label("weekly_digest") in text
    assert cycle_step_label("weekly_digest") == "Недельный дайджест"
    assert "boom" in text

    keyboard = build_disabled_steps_keyboard(vault_path)
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "menu:jobhealthenable:weekly_digest" in callback_data


def test_disabled_steps_screen_reports_all_clear_when_nothing_disabled(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    assert "все шаги" in build_disabled_steps_text(vault_path).lower()


# ---------------------------------------------------------------------------
# Task 19a: same-day rerun does not reclassify already-executed entries
# ---------------------------------------------------------------------------


def test_second_scheduled_run_of_the_same_day_does_not_recreate_tasks(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        (
            f"# {day.isoformat()}\n\n"
            "## 10:00 [text]\n"
            "Нужно отправить follow-up по проекту.\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    created_tasks: list[str] = []

    def fake_run(prompt: str) -> str:
        if "capture.md" in prompt:
            return json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [
                        {
                            "classification": "task",
                            "task_content": "Send follow-up",
                            "task_priority": 4,
                        }
                    ],
                    "stats": {"total_entries": 1, "tasks": 1, "skipped": 0},
                },
                ensure_ascii=False,
            )
        if "execute.md" in prompt:
            capture_json = json.loads(
                (vault_path / ".session" / "capture.json").read_text(
                    encoding="utf-8"
                )
            )
            tasks_created = []
            for entry in capture_json["entries"]:
                if entry.get("classification") == "task":
                    content = entry.get("task_content", "")
                    tasks_created.append({"content": content})
                    created_tasks.append(content)
            return json.dumps(
                {
                    "tasks_created": tasks_created,
                    "thoughts_saved": [],
                    "crm_updated": [],
                },
                ensure_ascii=False,
            )
        return "📊 **Обработка**"

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: None  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda *a, **k: None  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = lambda: None  # type: ignore[method-assign]
    processor._capture_memory_audit = lambda: None  # type: ignore[method-assign]

    first_result = processor.process_daily(day, mode=SCHEDULED_MODE)
    assert first_result["processed_entries"] == 1
    assert created_tasks == ["Send follow-up"]

    daily_text = (vault_path / "daily" / f"{day.isoformat()}.md").read_text(
        encoding="utf-8"
    )
    assert ENTRY_STATUS_ALREADY_PROCESSED in daily_text

    second_result = processor.process_daily(day, mode=SCHEDULED_MODE)
    assert second_result["processed_entries"] == 0
    # No second task got created for the same entry.
    assert created_tasks == ["Send follow-up"]


def test_new_entry_added_after_marked_entries_is_processed_alone_on_rerun(
    tmp_path: Path,
) -> None:
    # A same-day rerun must not touch entries already marked
    # already_processed, but a genuinely new entry appended afterwards
    # still has to be classified and executed.
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _setup_daily_processing_vault(vault_path, day)
    daily_file = vault_path / "daily" / f"{day.isoformat()}.md"
    daily_file.write_text(
        (
            f"# {day.isoformat()}\n\n"
            "## 09:00 [text]\n"
            "Send follow-up to client A.\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    created_tasks: list[str] = []

    def fake_run(prompt: str) -> str:
        if "capture.md" in prompt:
            daily_text = daily_file.read_text(encoding="utf-8")
            entries = []
            if "client A" in daily_text:
                entries.append(
                    {
                        "classification": "task",
                        "task_content": "Follow up A",
                        "task_priority": 4,
                    }
                )
            if "client B" in daily_text:
                entries.append(
                    {
                        "classification": "task",
                        "task_content": "Follow up B",
                        "task_priority": 4,
                    }
                )
            return json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": entries,
                    "stats": {
                        "total_entries": len(entries),
                        "tasks": len(entries),
                        "skipped": 0,
                    },
                },
                ensure_ascii=False,
            )
        if "execute.md" in prompt:
            capture_json = json.loads(
                (vault_path / ".session" / "capture.json").read_text(
                    encoding="utf-8"
                )
            )
            tasks_created = []
            for entry in capture_json["entries"]:
                if entry.get("classification") == "task":
                    content = entry.get("task_content", "")
                    tasks_created.append({"content": content})
                    created_tasks.append(content)
            return json.dumps(
                {
                    "tasks_created": tasks_created,
                    "thoughts_saved": [],
                    "crm_updated": [],
                },
                ensure_ascii=False,
            )
        return "📊 **Обработка**"

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: None  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda *a, **k: None  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = lambda: None  # type: ignore[method-assign]
    processor._capture_memory_audit = lambda: None  # type: ignore[method-assign]

    first_result = processor.process_daily(day, mode=SCHEDULED_MODE)
    assert first_result["processed_entries"] == 1
    assert created_tasks == ["Follow up A"]

    # Append a brand-new, unmarked entry after the one the guardrail will
    # now find already marked already_processed.
    with daily_file.open("a", encoding="utf-8") as handle:
        handle.write("\n## 11:00 [text]\nSend follow-up to client B.\n")

    second_result = processor.process_daily(day, mode=SCHEDULED_MODE)

    # Only the new entry (B) was classified/executed; A was not recreated.
    assert second_result["processed_entries"] == 1
    assert created_tasks == ["Follow up A", "Follow up B"]


# ---------------------------------------------------------------------------
# Task 19b: VERIFY phase warns about missing scheduled-cycle artifacts
# ---------------------------------------------------------------------------


def _daily_text_with_reflect_marker(day: date) -> str:
    return (
        f"# {day.isoformat()}\n\n"
        f"## 10:00 [d-brain]\n{REFLECT_DAILY_START_MARKER}\nok\n"
    )


def test_verify_scheduled_cycle_artifacts_warns_about_missing_handoff(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)

    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        _daily_text_with_reflect_marker(day),
        encoding="utf-8",
    )
    (vault_path / ".graph").mkdir(parents=True)
    (vault_path / ".graph" / "vault-graph.json").write_text("{}", encoding="utf-8")
    # No .session/handoff.md on disk -- the artifact under test.

    warnings = processor._verify_scheduled_cycle_artifacts(
        day, {"report": "ok", "processed_entries": 1}, []
    )

    assert any(".session/handoff.md" in line for line in warnings)
    assert any("VERIFY" in line for line in warnings)


def test_verify_scheduled_cycle_artifacts_silent_when_everything_present(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)

    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        _daily_text_with_reflect_marker(day),
        encoding="utf-8",
    )
    (vault_path / ".graph").mkdir(parents=True)
    (vault_path / ".graph" / "vault-graph.json").write_text("{}", encoding="utf-8")
    (vault_path / ".session").mkdir(parents=True)
    (vault_path / ".session" / "handoff.md").write_text("handoff", encoding="utf-8")

    warnings = processor._verify_scheduled_cycle_artifacts(
        day, {"report": "ok", "processed_entries": 1}, []
    )

    assert warnings == []


def test_verify_scheduled_cycle_artifacts_skips_daily_check_on_empty_day(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)
    (vault_path / ".graph").mkdir(parents=True)
    (vault_path / ".graph" / "vault-graph.json").write_text("{}", encoding="utf-8")
    (vault_path / ".session").mkdir(parents=True)
    (vault_path / ".session" / "handoff.md").write_text("handoff", encoding="utf-8")

    warnings = processor._verify_scheduled_cycle_artifacts(
        day, {"report": "", "processed_entries": 0, "empty_daily": True}, []
    )

    assert warnings == []


def test_verify_scheduled_cycle_artifacts_warns_about_missing_compiled_digest(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)

    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        _daily_text_with_reflect_marker(day),
        encoding="utf-8",
    )
    (vault_path / ".graph").mkdir(parents=True)
    (vault_path / ".graph" / "vault-graph.json").write_text("{}", encoding="utf-8")
    (vault_path / ".session").mkdir(parents=True)
    (vault_path / ".session" / "handoff.md").write_text("handoff", encoding="utf-8")

    periodic_cycles = [
        {
            "name": "maintenance.compiled-digest",
            "label": "digest",
            "result": {"report": "", "processed_entries": 1},
        }
    ]

    warnings = processor._verify_scheduled_cycle_artifacts(
        day, {"report": "ok", "processed_entries": 1}, periodic_cycles
    )

    assert any("дайджест" in line.lower() for line in warnings)


def test_run_scheduled_cycle_appends_verify_warnings_to_the_report(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 6)  # Monday: no periodic reviews
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)

    processor.process_daily = lambda d, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 0,
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }
    processor.audit_cycle_result = lambda **kwargs: {  # type: ignore[method-assign]
        "cycle_name": kwargs["cycle_name"],
        "label": kwargs["cycle_name"],
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    # Nothing wrote .session/handoff.md or .graph/vault-graph.json for real
    # here (process_daily is stubbed out above), so VERIFY must flag both.
    result = processor.run_scheduled_cycle(day)

    assert "VERIFY" in result["report"]
    assert ".session/handoff.md" in result["report"]
    assert ".graph/vault-graph.json" in result["report"]
