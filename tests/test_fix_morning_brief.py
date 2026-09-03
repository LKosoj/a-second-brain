"""Tests for the audit item 26 "morning brief" fix.

``build_morning_brief`` is pure and read-only: it never writes to the vault
and never sends Telegram, so these tests only build a temporary vault and
call it directly. The deploy-template tests below simulate the
``@PLACEHOLDER@`` substitution the install scripts perform, without
depending on their own test fixtures.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from _paths import PROJECT_ROOT

from d_brain.run_morning_brief import build_morning_brief

TODAY = date(2026, 9, 3)
YESTERDAY = date(2026, 9, 2)


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


def test_morning_brief_systemd_templates_render_at_08_00(tmp_path: Path) -> None:
    service_template = (
        PROJECT_ROOT / "deploy" / "a-second-brain-morning-brief.service.in"
    ).read_text(encoding="utf-8")
    timer_template = (
        PROJECT_ROOT / "deploy" / "a-second-brain-morning-brief.timer.in"
    ).read_text(encoding="utf-8")

    rendered_service = service_template.replace(
        "@PROJECT_DIR@", str(tmp_path)
    ).replace("@UV_BIN@", "/usr/bin/uv")

    assert "d_brain.run_morning_brief" in rendered_service
    assert "NoNewPrivileges=true" in rendered_service
    assert "@" not in rendered_service

    assert "OnCalendar=*-*-* 08:00:00" in timer_template
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
    )

    assert "d_brain.run_morning_brief" in rendered
    assert "<key>Hour</key>" in rendered
    assert "<integer>8</integer>" in rendered
    assert "<key>Minute</key>" in rendered
    assert "@" not in rendered
