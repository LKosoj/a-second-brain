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
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _write_vault_manifest

from d_brain import run_daily_process
from d_brain.services.processor import INTERACTIVE_MODE, SCHEDULED_MODE, CliProcessor


def test_scheduled_digest_shows_tasks_missing_from_todoist():
    text = run_daily_process._build_scheduled_digest(
        date(2026, 10, 15),
        {
            "daily": {"processed_entries": 2},
            "audit_tasks_unconfirmed": [{"content": "Проверить аудит"}],
            "retry_tasks_unconfirmed": [{"content": "Повтор"}],
        },
        {
            "tasks_pending": [{"content": "Позвонить"}],
            "tasks_unconfirmed": [{"content": "Написать"}],
        },
    )
    assert "Не отправлено в Todoist (ждёт повтора): **1**" in text
    assert "Не подтверждено, проверьте в Todoist: **3**" in text
    assert "ничего нового" not in text


def test_scheduled_digest_shows_daily_error_and_maintenance_sections():
    text = run_daily_process._build_scheduled_digest(
        date(2026, 10, 15),
        {
            "daily_error": "Processing timed out",
            "daily": {"error": "Processing timed out", "processed_entries": 0},
            "periodic_cycles": [
                {"label": "Здоровье vault", "result": {"error": "boom"}},
            ],
        },
        {},
    )
    assert "Ежедневная обработка: ошибка" in text
    assert "`Processing timed out`" in text
    assert "Здоровье vault: ошибка" in text
    assert "Ежедневная обработка завершена" not in text
    assert "ничего нового" not in text


def test_main_exits_nonzero_but_still_notifies_when_only_daily_failed(
    tmp_path, monkeypatch, capsys
):
    sent: list[tuple[str, bool]] = []

    class FakeProcessor:
        def run_scheduled_cycle(self, day):  # noqa: ANN001, ANN202
            return {
                "daily_error": "Processing timed out",
                "daily": {"error": "Processing timed out", "processed_entries": 0},
                "report": "ОТЧЁТ-ЦИКЛОВ",
                "periodic_cycles": [
                    {"label": "Здоровье vault", "result": {"status": "ok"}},
                ],
                "processed_entries": 0,
            }

    monkeypatch.setattr(
        run_daily_process,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="qwen",
            owner_full_name="Иван",
            content_language="ru",
            openai_api_key="",
            openai_base_url="",
            openai_model="",
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(run_daily_process, "CliProcessor", lambda *a: FakeProcessor())
    monkeypatch.setattr(
        run_daily_process,
        "_build_digest_takeaways",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append((text, rich)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Processing timed out" in captured.err
    assert "ОТЧЁТ-ЦИКЛОВ" in captured.out
    assert "Ежедневная обработка: ошибка" in sent[0][0]
    assert "Здоровье vault" in sent[0][0]
    assert sent[0][1] is True


def test_scheduled_digest_distinguishes_interval_skip_from_execution():
    text = run_daily_process._build_scheduled_digest(
        date(2026, 10, 15),
        {"periodic_cycles": [
            {"label": "Уход за вики", "result": {"status": "skipped-interval"}},
            {"label": "Здоровье", "result": {"status": "ok"}},
        ]},
        {},
    )
    assert "Уход за вики: пропущен" in text
    assert "Уход за вики: выполнен" not in text
    assert "Здоровье: выполнен" in text


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


@pytest.mark.parametrize(
    ("daily_report", "maintenance_report"),
    [
        ("", ""),
        ("", "Проверка сегодня выявила битые ссылки."),
        ("Сегодня сохранена новая мысль.", ""),
        ("Сегодня сохранена новая мысль.", "Проверка сегодня выявила битые ссылки."),
    ],
)
def test_digest_takeaways_excludes_archived_compiled_changes(
    tmp_path: Path,
    monkeypatch,
    daily_report: str,
    maintenance_report: str,
) -> None:
    archive_report = (
        "Дайджест обогащения — 2026-10-04\n"
        "Что изменилось: на встрече 13 февраля не выбрали подход к интеграции."
    )
    expected = "\n\n".join(
        part for part in (daily_report, maintenance_report) if part
    )
    captured: list[str] = []

    def summarize(self, *, day, report_markdown, execute_payload):  # noqa: ANN001, ANN202, ARG001
        captured.append(report_markdown)
        return ["Полезный вывод за сегодня."]

    monkeypatch.setattr(
        run_daily_process.ReflectionDigestService, "summarize", summarize
    )
    result = {
        "daily": {"report": daily_report, "empty_daily": not daily_report},
        "report": "\n\n".join(
            part for part in (daily_report, archive_report, maintenance_report) if part
        ),
        "periodic_cycles": [
            {
                "name": "maintenance.compiled-digest",
                "label": "Дайджест обогащения compiled",
                "result": {
                    "report": archive_report,
                    "summary_path": "summaries/compile/2026-10-04.md",
                },
            },
            {
                "name": "maintenance.vault-health",
                "result": {"report": maintenance_report},
            },
        ],
    }
    takeaways = run_daily_process._build_digest_takeaways(
        _fake_settings(tmp_path), date(2026, 10, 4), result
    )

    assert captured == ([expected] if expected else [])
    assert takeaways == (["Полезный вывод за сегодня."] if expected else [])
    digest = run_daily_process._build_scheduled_digest(
        date(2026, 10, 4), result, {}, takeaways=takeaways
    )
    assert "summaries/compile/2026-10-04.md" in digest
    assert "13 февраля" not in digest


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


def _freeze_now(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(run_daily_process, "owner_now", lambda: moment)


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


def test_resolve_processing_day_uses_daily_process_time_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timer moves with DAILY_PROCESS_TIME, so the catch-up cut-off must
    move with it or every early run would process yesterday."""
    monkeypatch.delenv("SCHEDULED_PROCESS_HOUR", raising=False)
    monkeypatch.setenv("DAILY_PROCESS_TIME", "06:30")

    _freeze_now(monkeypatch, datetime(2026, 9, 3, 7, 0))
    assert run_daily_process._resolve_processing_day(SCHEDULED_MODE) == date(
        2026, 9, 3
    )
    _freeze_now(monkeypatch, datetime(2026, 9, 3, 5, 0))
    assert run_daily_process._resolve_processing_day(SCHEDULED_MODE) == date(
        2026, 9, 2
    )


def test_scheduled_process_hour_env_beats_daily_process_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEDULED_PROCESS_HOUR", "7")
    monkeypatch.setenv("DAILY_PROCESS_TIME", "22:00")

    assert run_daily_process._read_scheduled_process_hour() == 7


@pytest.mark.parametrize("raw", ["25:00", "9", "abc", ""])
def test_invalid_daily_process_time_falls_back_to_21(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.delenv("SCHEDULED_PROCESS_HOUR", raising=False)
    monkeypatch.setenv("DAILY_PROCESS_TIME", raw)

    assert run_daily_process._read_scheduled_process_hour() == 21


def _run_main_recording_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    retry_error: Exception | None = None,
) -> list[str]:
    order: list[str] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            order.append("process")
            return {"report": "", "processed_entries": 0}

    def fake_retry(settings, day):  # noqa: ANN001, ANN202
        order.append("retry")
        if retry_error is not None:
            raise retry_error

    monkeypatch.setattr(
        run_daily_process, "get_settings", lambda: _fake_settings(tmp_path)
    )
    monkeypatch.setattr(
        run_daily_process, "CliProcessor", lambda *args: FakeProcessor()
    )
    monkeypatch.setattr(run_daily_process, "_retry_unrecognized_voice", fake_retry)
    monkeypatch.setattr(
        sys, "argv", ["run_daily_process.py", "--mode", mode, "--skip-notify"]
    )
    run_daily_process.main()
    return order


def test_main_retries_voice_before_processing_in_scheduled_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _run_main_recording_order(tmp_path, monkeypatch, SCHEDULED_MODE)
    assert order == ["retry", "process"]


def test_main_does_not_retry_voice_in_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _run_main_recording_order(tmp_path, monkeypatch, INTERACTIVE_MODE)
    assert order == ["process"]


def test_voice_retry_failure_does_not_stop_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The helper swallows a failure of the retry step itself.
    real = run_daily_process._retry_unrecognized_voice
    settings = _fake_settings(tmp_path)
    settings.deepgram_api_key = "key"

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("deepgram down")

    monkeypatch.setattr(run_daily_process, "retry_unrecognized_voice", boom)

    real(settings, date(2026, 9, 3))  # must not raise


def test_voice_retry_uses_fresh_transcriber_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each asyncio.run() makes a new event loop; a transcriber (and its HTTP
    # client) reused across files stays bound to the first, closed loop.
    import asyncio

    class LoopBoundTranscriber:
        def __init__(self, api_key: str, language: str) -> None:
            self.loop: asyncio.AbstractEventLoop | None = None

        async def transcribe(self, data: bytes) -> str:
            loop = asyncio.get_running_loop()
            if self.loop is None:
                self.loop = loop
            elif self.loop is not loop:
                raise RuntimeError("Event loop is closed")
            return "text"

    results: list[str] = []

    def fake_retry(vault, days, transcribe):  # noqa: ANN001, ANN202
        results.extend(transcribe(b"a") for _ in range(2))
        return SimpleNamespace(recognized=2, failed=0, skipped=0)

    monkeypatch.setattr(run_daily_process, "DeepgramTranscriber", LoopBoundTranscriber)
    monkeypatch.setattr(run_daily_process, "retry_unrecognized_voice", fake_retry)
    settings = _fake_settings(tmp_path)
    settings.deepgram_api_key = "key"

    run_daily_process._retry_unrecognized_voice(settings, date(2026, 9, 3))

    assert results == ["text", "text"]


def test_resolve_processing_day_uses_owner_hour_on_utc_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """18:30 UTC is 21:30 in Moscow: past the 21:00 run, so "today"; 17:30 UTC
    is 20:30 there, so the run is still the catch-up for yesterday."""
    from d_brain import clock

    clock.set_owner_timezone("Europe/Moscow")
    monkeypatch.setenv("DAILY_PROCESS_TIME", "21:00")
    monkeypatch.delenv("SCHEDULED_PROCESS_HOUR", raising=False)

    monkeypatch.setattr(
        clock, "_utcnow", lambda: datetime(2026, 9, 3, 18, 30, tzinfo=UTC)
    )
    assert run_daily_process._resolve_processing_day(SCHEDULED_MODE) == date(
        2026, 9, 3
    )

    monkeypatch.setattr(
        clock, "_utcnow", lambda: datetime(2026, 9, 3, 17, 30, tzinfo=UTC)
    )
    assert run_daily_process._resolve_processing_day(SCHEDULED_MODE) == date(
        2026, 9, 2
    )
