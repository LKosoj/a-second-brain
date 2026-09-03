"""Tests for the G13 run-daily audit fixes.

Covers:
A) run_daily_process.main() must always notify the owner even when building
   digest takeaways raises (the AI CLI call inside ``_build_digest_takeaways``
   is best-effort and must not swallow the notification with it).
B) The nightly cycle day must be pinned once at the start and threaded into
   every sub-cycle write, even if a long phase pushes the wall clock past
   midnight.
C) A systemd ``Persistent=true`` catch-up run (timer fires late, e.g. the
   next morning) must process yesterday instead of skipping straight to
   today.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _write_vault_manifest

from d_brain import run_daily_process
from d_brain.services.processor import INTERACTIVE_MODE, SCHEDULED_MODE, CliProcessor


def _fake_settings(vault_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=vault_path,
        todoist_api_key="",
        ai_cli="qwen",
        owner_full_name="Иван",
        content_language="ru",
        openai_api_key="",
        openai_base_url="",
        openai_model="",
        telegram_bot_token="bot-token",
        owner_telegram_id=42,
    )


# --- A) takeaways failure must not swallow the owner notification ---------


def test_main_notifies_with_fallback_when_takeaways_build_raises(
    tmp_path: Path, monkeypatch
) -> None:
    sent: list[tuple[str, bool]] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            assert day == date(2026, 4, 5)
            assert mode == SCHEDULED_MODE
            session_dir = tmp_path / ".session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "execute.json").write_text(
                json.dumps(
                    {
                        "tasks_created": [{"content": "Проверить дедлайн"}],
                        "thoughts_saved": [],
                        "crm_updated": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "report": "📊 **Обработка завершена**",
                "processed_entries": 3,
                "mode": SCHEDULED_MODE,
            }

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append((text, rich)),
    )

    def raising_takeaways(settings, day, result):  # noqa: ARG001
        raise RuntimeError("AI CLI timed out after 180s")

    monkeypatch.setattr(
        run_daily_process, "_build_digest_takeaways", raising_takeaways
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()

    assert exit_code == 0
    # The owner must still get exactly one notification, carrying the
    # fallback error line instead of silently losing the digest.
    assert len(sent) == 1
    digest_text, rich = sent[0]
    assert rich is True
    assert "выводы не сформированы" in digest_text
    assert "AI CLI timed out after 180s" in digest_text


def test_takeaways_fallback_does_not_hide_new_thoughts_section(
    tmp_path: Path, monkeypatch
) -> None:
    """The fallback warning used to be smuggled into the ``takeaways`` list,
    and ``_build_scheduled_digest`` hides "Новые мысли" whenever takeaways
    are non-empty (it assumes the LLM summary already covered them). A
    fallback error is not a real summary, so thoughts must still show up
    alongside the warning."""
    sent: list[str] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            del day, mode
            session_dir = tmp_path / ".session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "execute.json").write_text(
                json.dumps(
                    {
                        "tasks_created": [],
                        "thoughts_saved": [{"title": "Идея про recall"}],
                        "crm_updated": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {"report": "📊 done", "processed_entries": 1}

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append(text),
    )

    def raising_takeaways(settings, day, result):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(
        run_daily_process, "_build_digest_takeaways", raising_takeaways
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    run_daily_process.main()

    assert len(sent) == 1
    assert "выводы не сформированы" in sent[0]
    assert "**Новые мысли**" in sent[0]
    assert "Идея про recall" in sent[0]


def test_takeaways_fallback_truncates_long_exception_text(
    tmp_path: Path, monkeypatch
) -> None:
    sent: list[str] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            del day, mode
            session_dir = tmp_path / ".session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "execute.json").write_text(
                json.dumps(
                    {
                        "tasks_created": [{"content": "Проверить дедлайн"}],
                        "thoughts_saved": [],
                        "crm_updated": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {"report": "📊 done", "processed_entries": 1}

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append(text),
    )

    long_reason = "x" * 500

    def raising_takeaways(settings, day, result):  # noqa: ARG001
        raise RuntimeError(long_reason)

    monkeypatch.setattr(
        run_daily_process, "_build_digest_takeaways", raising_takeaways
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    run_daily_process.main()

    assert len(sent) == 1
    assert long_reason not in sent[0]
    assert "x" * 200 in sent[0]
    assert "x" * 201 not in sent[0]


def test_main_still_notifies_when_takeaways_ok_and_delivery_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Delivery errors are a separate failure mode and must not raise out of
    main() -- they are only logged."""

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            del day, mode
            return {"report": "📊 done", "processed_entries": 0}

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        run_daily_process, "_build_digest_takeaways", lambda *a, **k: []
    )

    def raising_notify(day, vault_path, result, *, takeaways=None):  # noqa: ARG001
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(
        run_daily_process, "_notify_scheduled_digest", raising_notify
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()

    assert exit_code == 0


# --- B) cycle day must be pinned across a midnight rollover ---------------


def test_log_periodic_summary_pins_entry_to_cycle_day_across_midnight(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)
    cycle_day = date(2026, 9, 3)
    # A long phase pushed the wall clock past midnight into the next day.
    late_timestamp = datetime(2026, 9, 4, 0, 12)
    summary_path = vault_path / "summaries" / "weekly" / "2026-09-03.md"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text("summary\n", encoding="utf-8")

    processor._log_periodic_summary(
        timestamp=late_timestamp,
        label="Weekly Digest",
        summary_path=summary_path,
        refresh_qmd=False,
        day=cycle_day,
    )

    cycle_daily = vault_path / "daily" / f"{cycle_day.isoformat()}.md"
    next_day_daily = vault_path / "daily" / "2026-09-04.md"
    assert cycle_daily.exists()
    assert not next_day_daily.exists()
    content = cycle_daily.read_text(encoding="utf-8")
    assert "Weekly Digest" in content
    # HH:MM in the entry still reflects the actual wall-clock time.
    assert "## 00:12" in content


def test_log_periodic_summary_without_day_keeps_old_behavior(
    tmp_path: Path,
) -> None:
    """Backward compatibility: omitting ``day`` must still file the entry
    under ``timestamp``'s own date, as before this fix."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)
    timestamp = datetime(2026, 9, 4, 0, 12)
    summary_path = vault_path / "summaries" / "weekly" / "2026-09-03.md"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text("summary\n", encoding="utf-8")

    processor._log_periodic_summary(
        timestamp=timestamp,
        label="Weekly Digest",
        summary_path=summary_path,
        refresh_qmd=False,
    )

    assert (vault_path / "daily" / "2026-09-04.md").exists()


def test_scheduled_cycle_day_resets_to_none_after_run(tmp_path: Path) -> None:
    """``_scheduled_cycle_day`` is only meant to live for the duration of one
    cycle; a stale value left behind could leak into an unrelated later call
    to ``_run_compiled_digest_cycle`` on the same processor instance."""
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 0,
        "mode": mode,
    }
    processor.audit_cycle_result = lambda *, cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "label": cycle_name,
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    # Wednesday, not a month/year end -- no periodic sub-cycles triggered.
    processor.run_scheduled_cycle(date(2026, 8, 5))

    assert processor._scheduled_cycle_day is None


def test_compiled_digest_cycle_uses_pinned_scheduled_cycle_day(
    tmp_path: Path, monkeypatch
) -> None:
    """``_run_compiled_digest_cycle`` is dispatched by name with no
    arguments from the scheduled maintenance loop, so it must fall back to
    the cycle day pinned on the processor instance instead of
    ``date.today()`` when a phase crosses midnight."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    processor = CliProcessor(vault_path)
    cycle_day = date(2020, 5, 17)
    processor._scheduled_cycle_day = cycle_day

    captured_days: list[date] = []

    monkeypatch.setattr(
        "d_brain.services.processor.load_manifest_for_vault",
        lambda vault_path: object(),
    )
    monkeypatch.setattr(
        "d_brain.services.processor.read_pass_status",
        lambda vault_path: {},
    )

    def fake_build_daily_digest(vault_path, day, *, pass_status):  # noqa: ARG001
        captured_days.append(day)
        return None  # quiet night: no further writes needed for this test

    monkeypatch.setattr(
        "d_brain.services.processor.build_daily_digest",
        fake_build_daily_digest,
    )

    result = processor._run_compiled_digest_cycle()

    assert captured_days == [cycle_day]
    assert result["skipped"] is True


# --- C) systemd Persistent=true catch-up must process yesterday -----------


class _FixedDatetime(datetime):
    _fixed_now: datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        return cls._fixed_now


def _freeze_now(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    frozen = type("FrozenDatetime", (_FixedDatetime,), {"_fixed_now": moment})
    monkeypatch.setattr(run_daily_process, "datetime", frozen)


def test_resolve_processing_day_before_scheduled_hour_is_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 9, 0))

    day = run_daily_process._resolve_processing_day(SCHEDULED_MODE)

    assert day == date(2026, 9, 2)


def test_resolve_processing_day_at_scheduled_hour_is_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 21, 5))

    day = run_daily_process._resolve_processing_day(SCHEDULED_MODE)

    assert day == date(2026, 9, 3)


def test_resolve_processing_day_respects_custom_env_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 8, 0))
    monkeypatch.setenv("SCHEDULED_PROCESS_HOUR", "7")

    day = run_daily_process._resolve_processing_day(SCHEDULED_MODE)

    assert day == date(2026, 9, 3)  # 8 >= 7: not a catch-up anymore


@pytest.mark.parametrize("raw_hour", ["abc", "", "24", "-1"])
def test_resolve_processing_day_falls_back_on_invalid_env_hour(
    monkeypatch: pytest.MonkeyPatch, raw_hour: str
) -> None:
    """An unparseable or out-of-range ``SCHEDULED_PROCESS_HOUR`` must not
    crash the process before the owner is notified -- fall back to the
    default hour instead."""
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 9, 0))
    monkeypatch.setenv("SCHEDULED_PROCESS_HOUR", raw_hour)

    # 09:00 is before the default hour (21), so this must still resolve to
    # a catch-up for yesterday rather than raising.
    day = run_daily_process._resolve_processing_day(SCHEDULED_MODE)

    assert day == date(2026, 9, 2)


def test_resolve_processing_day_interactive_mode_ignores_scheduled_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 5, 0))

    day = run_daily_process._resolve_processing_day(INTERACTIVE_MODE)

    assert day == date(2026, 9, 3)


def test_main_explicit_date_overrides_catchup_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 9, 0))

    seen_days: list[date] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            del mode
            seen_days.append(day)
            return {"report": "", "processed_entries": 0}

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_daily_process.py",
            "--mode",
            "scheduled",
            "--date",
            "2026-08-01",
            "--skip-notify",
        ],
    )

    run_daily_process.main()

    assert seen_days == [date(2026, 8, 1)]


def test_main_scheduled_catchup_without_explicit_date_processes_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 9, 0))

    seen_days: list[date] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            del mode
            seen_days.append(day)
            return {"report": "", "processed_entries": 0}

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--skip-notify"],
    )

    run_daily_process.main()

    assert seen_days == [date(2026, 9, 2)]
