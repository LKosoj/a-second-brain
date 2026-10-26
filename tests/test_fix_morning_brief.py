"""Tests for the audit item 26 "morning brief" fix.

``build_morning_brief`` is pure and read-only: it never writes to the vault
and never sends Telegram, so these tests only build a temporary vault and
call it directly. The deploy-template tests below simulate the
``@PLACEHOLDER@`` substitution the install scripts perform, without
depending on their own test fixtures.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from _paths import PROJECT_ROOT

from d_brain import run_morning_brief
from d_brain.run_morning_brief import build_morning_brief
from d_brain.services.todoist_tasks import TodoistTask, TodoistTaskError

TODAY = date(2026, 9, 3)
YESTERDAY = date(2026, 9, 2)
MONDAY = date(2026, 10, 26)


def _write_daily(vault_path: Path, day: date, content: str) -> None:
    daily_dir = vault_path / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / f"{day.isoformat()}.md").write_text(content, encoding="utf-8")


def _write_weekly(vault_path: Path, content: str) -> None:
    goals_dir = vault_path / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / "3-weekly.md").write_text(content, encoding="utf-8")


def test_build_morning_brief_lists_unclosed_checkboxes_and_one_big_thing(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(
        vault_path,
        YESTERDAY,
        (
            "## 10:00 [text]\n"
            "- [ ] Отправить отчёт клиенту\n"
            "- [x] Уже сделано, не должно попасть в бриф\n"
            "- [ ] Позвонить в банк\n"
        ),
    )
    _write_weekly(
        vault_path,
        (
            "## ONE Big Thing\n\n"
            "> **If I accomplish nothing else, I will:**\n"
            "> Закрыть контракт с клиентом X\n"
        ),
    )

    brief = build_morning_brief(vault_path, TODAY)

    assert "Отправить отчёт клиенту" in brief
    assert "Позвонить в банк" in brief
    assert "Уже сделано" not in brief
    assert "Закрыть контракт с клиентом X" in brief


def test_build_morning_brief_ignores_checkboxes_inside_code_fences(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(
        vault_path,
        YESTERDAY,
        (
            "## 10:00 [text]\n"
            "```\n"
            "- [ ] пример синтаксиса, не задача\n"
            "```\n"
            "- [ ] Настоящая задача\n"
        ),
    )

    brief = build_morning_brief(vault_path, TODAY)

    assert "Настоящая задача" in brief
    assert "пример синтаксиса" not in brief


def test_build_morning_brief_reads_template_style_one_big_thing(
    tmp_path: Path,
) -> None:
    """The distributed template shape (``## One big thing`` + a bullet) --
    see ``src/d_brain/resources/vault_template/goals/3-weekly.md`` -- must
    also be recognised, not only the processor's promoted blockquote."""
    vault_path = tmp_path / "vault"
    _write_weekly(vault_path, "## One big thing\n\n- Запустить новую фичу\n")

    brief = build_morning_brief(vault_path, TODAY)

    assert "Запустить новую фичу" in brief


def test_build_morning_brief_excludes_html_comment_after_promoted_focus(
    tmp_path: Path,
) -> None:
    """Real post-rollover shape (see ``CliProcessor._promote_next_week_focus``
    and ``tests/test_processor_cycles.py``): a bot-only HTML comment sits
    between the quoted focus and the ``---`` divider and must not leak into
    the brief."""
    vault_path = tmp_path / "vault"
    _write_weekly(
        vault_path,
        (
            "---\n"
            "type: weekly\n"
            "updated: 2026-05-08\n"
            "last_accessed: 2026-05-08\n"
            "week: 2026-W19\n"
            "---\n\n"
            "# Weekly Focus\n\n"
            "## ONE Big Thing\n\n"
            "> **If I accomplish nothing else, I will:**\n"
            "> Старый фокус недели.\n\n"
            "<!-- This is read by the bot during daily processing -->\n\n"
            "---\n\n"
            "## Week at a Glance\n\n"
            "**Week:** 19 of 53\n\n"
            "---\n\n"
            "*Week Started: 2026-05-04*\n"
        ),
    )

    brief = build_morning_brief(vault_path, TODAY)

    assert "Старый фокус недели." in brief
    assert "<!--" not in brief
    assert "read by the bot" not in brief


def test_build_morning_brief_ignores_placeholder_one_big_thing(
    tmp_path: Path,
) -> None:
    """A bullet whose only content is markup/ellipsis noise (``- …``, ``_( )_``)
    is not real text and must be treated the same as the bare ``-``
    placeholder."""
    vault_path = tmp_path / "vault"
    _write_weekly(vault_path, "## One big thing\n\n- _( )_\n")

    brief = build_morning_brief(vault_path, TODAY)

    assert brief == "Сегодня без хвостов"


def test_build_morning_brief_empty_day_returns_no_tails_message(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "goals").mkdir(parents=True)
    _write_weekly(vault_path, "## One big thing\n\n-\n")

    brief = build_morning_brief(vault_path, TODAY)

    assert brief == "Сегодня без хвостов"


def test_build_morning_brief_survives_vanished_compiled_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, YESTERDAY, "## 10:00 [text]\n- [ ] Позвонить в банк\n")

    def vanished(_vault_path: Path) -> list[object]:
        raise FileNotFoundError("compiled/people/ivan.md")

    monkeypatch.setattr("d_brain.run_morning_brief.list_queue_items", vanished)

    brief = build_morning_brief(vault_path, TODAY)

    assert "Позвонить в банк" in brief


def test_build_morning_brief_limits_todoist_overdue_to_three(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    overdue = [f"Задача {n}" for n in range(1, 6)]

    brief = build_morning_brief(vault_path, TODAY, todoist_overdue=overdue)

    for shown in overdue[:3]:
        assert shown in brief
    for hidden in overdue[3:]:
        assert hidden not in brief


def test_build_morning_brief_ignores_empty_todoist_overdue(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"

    brief = build_morning_brief(vault_path, TODAY, todoist_overdue=[])

    assert brief == "Сегодня без хвостов"


def _due(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _fake_todoist(tasks: list[TodoistTask] | Exception):
    class FakeTodoist:
        def __init__(self, *_args: object) -> None:
            pass

        def today(self) -> tuple[str, list[TodoistTask]]:
            if isinstance(tasks, Exception):
                raise tasks
            return "UTC", tasks

    return FakeTodoist


def test_todoist_overdue_returns_only_overdue_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = [
        TodoistTask("a", "Вторая по важности", _due(3), "p2"),
        TodoistTask("b", "Самая важная", _due(5), "p1"),
        TodoistTask("c", "На будущее", _due(-10), "p1"),
    ]
    monkeypatch.setattr(run_morning_brief, "TodoistTasks", _fake_todoist(tasks))
    settings = SimpleNamespace(todoist_api_key="key", vault_path=tmp_path)

    result = run_morning_brief._todoist_overdue(settings)

    assert result == [
        f"Самая важная ({_due(5)})",
        f"Вторая по важности ({_due(3)})",
    ]


def test_todoist_overdue_without_key_skips_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def forbidden(*_args: object) -> None:
        raise AssertionError("client must not be created without a key")

    monkeypatch.setattr(run_morning_brief, "TodoistTasks", forbidden)
    settings = SimpleNamespace(todoist_api_key="  ", vault_path=tmp_path)

    with caplog.at_level(logging.INFO, logger="d_brain.run_morning_brief"):
        assert run_morning_brief._todoist_overdue(settings) == []

    assert "no API key" in caplog.text


def test_todoist_overdue_survives_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        run_morning_brief, "TodoistTasks", _fake_todoist(TodoistTaskError("boom"))
    )
    settings = SimpleNamespace(todoist_api_key="key", vault_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="d_brain.run_morning_brief"):
        assert run_morning_brief._todoist_overdue(settings) == []

    assert "boom" in caplog.text


def test_main_passes_overdue_to_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, date(2026, 9, 2), "## 10:00 [text]\n- [ ] Позвонить\n")
    send = Mock()
    monkeypatch.setattr(
        run_morning_brief,
        "get_settings",
        lambda: SimpleNamespace(vault_path=vault_path, todoist_api_key="key"),
    )
    monkeypatch.setattr(
        run_morning_brief,
        "_todoist_overdue",
        lambda _settings: ["Вернуть книгу (2026-10-20)"],
    )
    monkeypatch.setattr(run_morning_brief, "send_telegram_text_sync", send)
    monkeypatch.setattr(sys, "argv", ["x", "--date", "2026-09-03"])

    assert run_morning_brief.main() == 0

    out = capsys.readouterr().out
    assert "Просроченные задачи Todoist" in out
    assert "Вернуть книгу (2026-10-20)" in out
    assert send.call_args.args[0] == out.rstrip("\n")


def test_main_sends_brief_when_todoist_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, date(2026, 9, 2), "## 10:00 [text]\n- [ ] Позвонить\n")
    send = Mock()
    monkeypatch.setattr(
        run_morning_brief,
        "get_settings",
        lambda: SimpleNamespace(vault_path=vault_path, todoist_api_key=""),
    )
    monkeypatch.setattr(run_morning_brief, "send_telegram_text_sync", send)
    monkeypatch.setattr(sys, "argv", ["x", "--date", "2026-09-03"])

    assert run_morning_brief.main() == 0

    text = send.call_args.args[0]
    assert "Позвонить" in text
    assert "Просроченные задачи Todoist" not in text


def _monday_vault(tmp_path: Path) -> Path:
    """Vault with a yesterday checkbox so the brief is not "no tails"."""
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, MONDAY - timedelta(days=1), "- [ ] Позвонить в банк\n")
    return vault_path


def test_on_this_day_shown_on_monday_with_link(tmp_path: Path) -> None:
    assert MONDAY.weekday() == 0
    vault_path = _monday_vault(tmp_path)
    _write_daily(
        vault_path,
        date(2025, 10, 26),
        "## 09:00 [text]\nПробный запуск проекта Пример\n",
    )

    brief = build_morning_brief(vault_path, MONDAY)

    assert "**Что было в этот день**" in brief
    assert "Пробный запуск проекта Пример" in brief
    assert "[[daily/2025-10-26]]" in brief


def test_on_this_day_absent_on_other_weekdays(tmp_path: Path) -> None:
    tuesday = MONDAY + timedelta(days=1)
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, MONDAY, "- [ ] Позвонить в банк\n")
    _write_daily(vault_path, date(2025, 10, 27), "## 09:00 [text]\nСтарая запись\n")

    brief = build_morning_brief(vault_path, tuesday)

    assert "Позвонить в банк" in brief
    assert "Что было в этот день" not in brief
    assert "Старая запись" not in brief


def test_on_this_day_prefers_year_ago_and_caps_at_two(tmp_path: Path) -> None:
    vault_path = _monday_vault(tmp_path)
    for day, text in [
        (date(2026, 9, 26), "Месяц назад"),
        (date(2026, 8, 26), "Два месяца назад"),
        (date(2026, 7, 26), "Три месяца назад"),
        (date(2025, 10, 26), "Год назад"),
    ]:
        _write_daily(vault_path, day, f"## 09:00 [text]\n{text}\n")

    brief = build_morning_brief(vault_path, MONDAY)

    assert "Год назад" in brief
    assert "Месяц назад" in brief
    assert "Два месяца назад" not in brief
    assert "Три месяца назад" not in brief


def test_on_this_day_skips_photo_only_and_marker_entries(tmp_path: Path) -> None:
    vault_path = _monday_vault(tmp_path)
    _write_daily(
        vault_path,
        date(2025, 10, 26),
        (
            "## 08:00 [photo]\n"
            "<!-- d-brain:entry-status processed -->\n"
            "![[attachments/2025-10-26/photo.jpg]]\n\n"
            "## 09:00 [text]\n"
            "Текст после фото\n"
        ),
    )
    _write_daily(
        vault_path,
        date(2026, 9, 26),
        "## 08:00 [photo]\n![[attachments/2026-09-26/photo.jpg]]\n",
    )

    brief = build_morning_brief(vault_path, MONDAY)

    assert "Текст после фото" in brief
    assert "attachments" not in brief
    assert "entry-status" not in brief
    assert "[[daily/2026-09-26]]" not in brief


def test_on_this_day_truncates_long_snippet(tmp_path: Path) -> None:
    vault_path = _monday_vault(tmp_path)
    _write_daily(vault_path, date(2025, 10, 26), "## 09:00 [text]\n" + "я" * 500 + "\n")

    brief = build_morning_brief(vault_path, MONDAY)

    line = next(item for item in brief.splitlines() if "[[daily/2025-10-26]]" in item)
    limit = run_morning_brief.MAX_ON_THIS_DAY_CHARS
    assert "я" * (limit - 1) + "…" in line
    assert "я" * limit not in line


def test_on_this_day_skips_impossible_dates(tmp_path: Path) -> None:
    day = date(2026, 3, 30)
    assert day.weekday() == 0
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, day - timedelta(days=1), "- [ ] Позвонить в банк\n")
    _write_daily(vault_path, date(2026, 1, 30), "## 09:00 [text]\nЯнварская запись\n")

    brief = build_morning_brief(vault_path, day)

    assert "Январская запись" in brief


def test_on_this_day_skips_undecodable_daily_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault_path = _monday_vault(tmp_path)
    broken = vault_path / "daily" / "2025-10-26.md"
    broken.write_bytes(b"## 09:00 [text]\n\xff\xfe\xfa broken\n")
    _write_daily(
        vault_path, date(2026, 9, 26), "## 09:00 [text]\nЦелая запись\n"
    )

    with caplog.at_level(logging.WARNING):
        brief = build_morning_brief(vault_path, MONDAY)

    assert "Целая запись" in brief
    assert "[[daily/2025-10-26]]" not in brief
    assert "2025-10-26" in caplog.text


def test_on_this_day_snippet_markup_does_not_reach_brief(tmp_path: Path) -> None:
    from d_brain.bot.replies import _telegram_message_payload

    vault_path = _monday_vault(tmp_path)
    _write_daily(
        vault_path,
        date(2025, 10, 26),
        "## 09:00 [text]\n*жирный* _курсив_ `код` [текст] [x](http://a.b)\n",
    )

    brief = build_morning_brief(vault_path, MONDAY)

    line = next(item for item in brief.splitlines() if "[[daily/2025-10-26]]" in item)
    snippet = line.split(": ", 1)[1].removesuffix(" [[daily/2025-10-26]]")
    assert not set("*_`[]") & set(snippet)
    # The wikilink survives as visible text instead of breaking the payload.
    payload, parse_mode = _telegram_message_payload(brief, parse_mode=None)
    assert parse_mode == "MarkdownV2"
    assert "\\[\\[daily/2025\\-10\\-26\\]\\]" in payload
    assert "жирный курсив код текст" in payload


def test_monday_with_no_tails_stays_short(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_daily(vault_path, date(2025, 10, 26), "## 09:00 [text]\nСтарая запись\n")

    assert build_morning_brief(vault_path, MONDAY) == "Сегодня без хвостов"


def test_morning_brief_systemd_templates_render_at_08_00(tmp_path: Path) -> None:
    service_template = (
        PROJECT_ROOT / "deploy" / "a-second-brain-morning-brief.service.in"
    ).read_text(encoding="utf-8")
    timer_template = (
        PROJECT_ROOT / "deploy" / "a-second-brain-morning-brief.timer.in"
    ).read_text(encoding="utf-8")

    rendered_service = service_template.replace(
        "@PROJECT_DIR@", str(tmp_path)
    ).replace("@UV_BIN@", "/usr/bin/uv").replace("@TZ_ENVIRONMENT@", "")

    assert "d_brain.run_morning_brief" in rendered_service
    assert "NoNewPrivileges=true" in rendered_service
    assert "@" not in rendered_service

    # The installer substitutes the time from MORNING_BRIEF_TIME (default 08:00).
    assert "OnCalendar=*-*-* @MORNING_BRIEF_TIME@:00" in timer_template
    assert "Persistent=true" in timer_template


def test_morning_brief_launchd_plist_renders_at_hour_8(tmp_path: Path) -> None:
    plist_template = (
        PROJECT_ROOT / "deploy" / "com.second-brain.morning-brief.plist.in"
    ).read_text(encoding="utf-8")

    rendered = (
        plist_template.replace("@PROJECT_DIR@", str(tmp_path))
        .replace("@UV_BIN@", "/usr/bin/uv")
        .replace("@WRAPPER@", "/usr/bin/env")
        .replace("@LOG_DIR@", str(tmp_path / "logs"))
        .replace("@MORNING_BRIEF_HOUR@", "8")
        .replace("@MORNING_BRIEF_MINUTE@", "0")
        .replace("@TZ_PLIST_ENTRY@", "")
    )

    assert "d_brain.run_morning_brief" in rendered
    assert "<key>Hour</key>" in rendered
    assert "<integer>8</integer>" in rendered
    assert "<key>Minute</key>" in rendered
    assert "@" not in rendered
