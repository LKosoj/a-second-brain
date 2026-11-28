"""Tests for Stream 3B dashboard, queue, reminders, learning, goal, and meetings."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User

from d_brain.bot import dashboard
from d_brain.bot.handlers import menu as menu_handler
from d_brain.clock import owner_now, owner_today
from d_brain.run_morning_brief import build_morning_brief
from d_brain.services.calendar_ics import CalendarEvent, CalendarResult
from d_brain.services.conversation_learning import (
    LearningLesson,
    delete_lesson,
    list_lessons,
)
from d_brain.services.decisions_queue import (
    CONFLICT_KIND,
    DRIFT_KIND,
    DUPLICATE_CANDIDATE_KIND,
    FACT_CHECK_REJECTED_KIND,
    HUMAN_ZONE_AMBIGUOUS_KIND,
    OWNER_DECISION_GROUP,
    PAGE_ENCODING_BROKEN_KIND,
    TECHNICAL_ATTENTION_GROUP,
    VERIFY_REJECTED_KIND,
    QueueItem,
    action_button_specs,
    partition_queue_items,
    queue_item_group,
    render_queue_document,
)
from d_brain.services.file_browser import BrowserEntry
from d_brain.services.reminders import Reminder, add_reminder, mark_sent

# --- 1. Persistent DashboardSession ---------------------------------------


def test_dashboard_session_persists_and_restores_after_restart(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    chat_id = 777001
    dashboard.clear_dashboard_session(chat_id)

    session = dashboard.get_dashboard_session(chat_id, vault_path=vault)
    session.dashboard_message_id = 123
    session.current_screen = "files"
    session.file_root_id = "daily"
    session.file_current_dir = "2026"
    session.file_page = 2
    session.file_entries = [
        BrowserEntry(
            name="note.md", relative_path="2026/note.md", is_directory=False, size=42
        )
    ]
    session.queue_page = 1
    session.queue_items = [
        QueueItem(
            kind=CONFLICT_KIND,
            page="compiled/topics/a.md",
            summary="old vs new",
            since="2026-08-01",
            conflict_row=("2026-08-01", "old", "daily/1.md", "new", "daily/2.md"),
        )
    ]
    session.learning_lessons = [
        LearningLesson(
            lesson_id="2026-08-01-user-correction-1-2",
            rel_path="thoughts/learnings/2026-08-01-user-correction-1-2.md",
            kind="user-correction",
            created="2026-08-01",
            condition="when testing",
            text="use flag X",
            outcome="",
            source="telegram:1:2",
        )
    ]
    dashboard.save_dashboard_session(vault, session)

    # Simulate process restart: clear in-memory cache only.
    dashboard.clear_dashboard_session(chat_id)
    restored = dashboard.get_dashboard_session(chat_id, vault_path=vault)

    assert restored.dashboard_message_id == 123
    assert restored.current_screen == "files"
    assert restored.file_root_id == "daily"
    assert restored.file_current_dir == "2026"
    assert restored.file_page == 2
    assert restored.file_entries == [
        BrowserEntry(
            name="note.md", relative_path="2026/note.md", is_directory=False, size=42
        )
    ]
    assert restored.queue_page == 1
    assert len(restored.queue_items) == 1
    assert restored.queue_items[0].conflict_row == (
        "2026-08-01",
        "old",
        "daily/1.md",
        "new",
        "daily/2.md",
    )
    assert len(restored.learning_lessons) == 1
    assert restored.learning_lessons[0].lesson_id == "2026-08-01-user-correction-1-2"

    # Clearing with vault_path removes persisted entry from disk too.
    dashboard.clear_dashboard_session(chat_id, vault_path=vault)
    persisted = json.loads(
        (vault / ".session" / "dashboard-sessions.json").read_text(encoding="utf-8")
    )
    assert str(chat_id) not in persisted


# --- 2. Reminders in morning brief & home screen --------------------------


def test_morning_brief_shows_only_today_pending_reminders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    today = date(2026, 9, 3)
    due_morning = datetime(2026, 9, 3, 9, 30, tzinfo=owner_now().tzinfo)
    due_evening = datetime(2026, 9, 3, 18, 0, tzinfo=owner_now().tzinfo)
    due_tomorrow = datetime(2026, 9, 4, 10, 0, tzinfo=owner_now().tzinfo)

    add_reminder(vault, 1, Reminder(due_at=due_evening, text="Вечерний звонок"))
    add_reminder(vault, 1, Reminder(due_at=due_morning, text="Оплатить счёт"))
    sent_rec = add_reminder(
        vault, 1, Reminder(due_at=due_morning, text="Уже отправлено")
    )
    mark_sent(vault, sent_rec.id)
    add_reminder(vault, 1, Reminder(due_at=due_tomorrow, text="Завтрашнее дело"))

    brief = build_morning_brief(vault, today)

    assert "Напоминания на сегодня" in brief
    assert "09:30 — Оплатить счёт" in brief
    assert "18:00 — Вечерний звонок" in brief
    assert brief.index("Оплатить счёт") < brief.index("Вечерний звонок")
    assert "Уже отправлено" not in brief
    assert "Завтрашнее дело" not in brief


def test_build_home_text_includes_today_reminders_when_present(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    now = owner_now()
    due_today = now.replace(hour=15, minute=45, second=0, microsecond=0)
    add_reminder(vault, 1, Reminder(due_at=due_today, text="Проверить релиз"))

    text = dashboard.build_home_text(vault, 1)

    assert "Напоминания сегодня" in text
    assert "15:45 — Проверить релиз" in text


def test_build_home_text_omits_reminders_block_when_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    text = dashboard.build_home_text(vault, 1)

    assert "Напоминания сегодня" not in text


# --- 3. Decisions queue split & UI labels ---------------------------------


def test_decisions_queue_splits_owner_and_technical_items() -> None:
    owner = [
        QueueItem(
            kind=CONFLICT_KIND, page="compiled/a.md", summary="c", since="2026-08-01"
        ),
        QueueItem(
            kind=FACT_CHECK_REJECTED_KIND,
            page="compiled/b.md",
            summary="f",
            since="2026-08-01",
        ),
        QueueItem(
            kind=DUPLICATE_CANDIDATE_KIND,
            page="compiled/c.md",
            summary="d",
            since="2026-08-01",
        ),
    ]
    technical = [
        QueueItem(
            kind=PAGE_ENCODING_BROKEN_KIND,
            page="compiled/d.md",
            summary="e",
            since="2026-08-01",
        ),
        QueueItem(
            kind=HUMAN_ZONE_AMBIGUOUS_KIND,
            page="compiled/e.md",
            summary="h",
            since="2026-08-01",
        ),
        QueueItem(
            kind=VERIFY_REJECTED_KIND,
            page="compiled/f.md",
            summary="v",
            since="2026-08-01",
        ),
        QueueItem(
            kind=DRIFT_KIND, page="compiled/g.md", summary="dr", since="2026-08-01"
        ),
    ]

    for item in owner:
        assert queue_item_group(item) == OWNER_DECISION_GROUP
    for item in technical:
        assert queue_item_group(item) == TECHNICAL_ATTENTION_GROUP

    grouped_owner, grouped_tech = partition_queue_items([*technical, *owner])
    assert grouped_owner == owner
    assert grouped_tech == technical

    screen_text = dashboard.build_queue_text(
        items=[*owner, *technical], page=0, total_pages=1, total_items=7
    )
    assert "**Решения владельца**" in screen_text
    assert "**Техническое внимание**" in screen_text
    assert screen_text.index("Решения владельца") < screen_text.index(
        "Техническое внимание"
    )

    doc_text = render_queue_document([*technical, *owner])
    assert "## Решения владельца" in doc_text
    assert "## Техническое внимание" in doc_text
    assert doc_text.index("## Решения владельца") < doc_text.index(
        "## Техническое внимание"
    )

    # Dashboard UI labels and decisions_queue specs expose the same action ids.
    for item in [*owner, *technical]:
        ui_ids = [aid for aid, _ in dashboard.queue_action_button_specs(item)]
        svc_ids = [aid for aid, _ in action_button_specs(item)]
        assert ui_ids == svc_ids


# --- 4. Transparent Conversation Learning ---------------------------------


def _write_lesson_card(
    vault: Path, name: str, *, condition: str, field: str, text: str
) -> Path:
    learnings_dir = vault / "thoughts" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    path = learnings_dir / name
    path.write_text(
        "---\n"
        'type: "note"\n'
        f'condition: "{condition}"\n'
        f'{field}: "{text}"\n'
        'source: "telegram:1:10"\n'
        'created: "2026-08-10"\n'
        "---\n\n# Card\n",
        encoding="utf-8",
    )
    return path


def test_list_and_delete_conversation_lessons(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_lesson_card(
        vault,
        "2026-08-09-user-correction-1-10.md",
        condition="При импорте CSV",
        field="lesson",
        text="Использовать разделитель точка с запятой",
    )
    _write_lesson_card(
        vault,
        "2026-08-10-confirmed-practice-1-20.md",
        condition="При сборке релиза",
        field="practice",
        text="Запускать pytest перед тегом",
    )
    # Non-lesson markdown file must not be listed or deletable.
    other = vault / "thoughts" / "learnings" / "manual-note.md"
    other.write_text("---\ntype: note\n---\nmanual\n", encoding="utf-8")

    lessons = list_lessons(vault)
    assert [lesson.lesson_id for lesson in lessons] == [
        "2026-08-10-confirmed-practice-1-20",
        "2026-08-09-user-correction-1-10",
    ]
    assert lessons[0].kind == "confirmed-practice"
    assert lessons[1].kind == "user-correction"

    assert delete_lesson(vault, "../manual-note") is False
    assert delete_lesson(vault, "manual-note") is False
    assert other.exists()

    assert delete_lesson(vault, "2026-08-09-user-correction-1-10") is True
    assert delete_lesson(vault, "2026-08-09-user-correction-1-10") is False
    assert [lesson.lesson_id for lesson in list_lessons(vault)] == [
        "2026-08-10-confirmed-practice-1-20"
    ]


@pytest.mark.asyncio
async def test_learning_menu_callback_deletes_lesson_and_rejects_stale_fp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_lesson_card(
        vault,
        "2026-08-10-user-correction-1-10.md",
        condition="Когда пишу отчёт",
        field="lesson",
        text="Добавлять итог в начале",
    )
    chat_id = 777002
    dashboard.clear_dashboard_session(chat_id)
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path=vault)
    )
    rendered_notices: list[str | None] = []

    async def fake_render_learnings(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        rendered_notices.append(notice)

    monkeypatch.setattr(menu_handler, "render_learnings", fake_render_learnings)

    session = dashboard.get_dashboard_session(chat_id)
    lessons = list_lessons(vault)
    session.learning_lessons = lessons

    # Stale fingerprint rejected without deleting.
    stale_query = SimpleNamespace(
        data="menu:learningdel:0:deadbeef",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=50),
        from_user=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )
    await menu_handler.handle_menu_callback(stale_query, object(), AsyncMock())
    assert len(list_lessons(vault)) == 1
    assert stale_query.answer.await_args.kwargs["show_alert"] is True

    # Valid fingerprint deletes the lesson.
    fp = dashboard.lesson_fingerprint(lessons[0].lesson_id)
    valid_query = SimpleNamespace(
        data=f"menu:learningdel:0:{fp}",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=50),
        from_user=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )
    await menu_handler.handle_menu_callback(valid_query, object(), AsyncMock())
    assert list_lessons(vault) == []
    assert rendered_notices == ["Урок удалён."]


# --- 5. Weekly ONE Big Thing view & edit ----------------------------------


def test_update_weekly_one_big_thing_replaces_quote_and_preserves_rest(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    goals = vault / "goals"
    goals.mkdir(parents=True)
    weekly = goals / "3-weekly.md"
    weekly.write_text(
        "---\n"
        "type: weekly\n"
        "week: 2026-W32\n"
        "---\n\n"
        "# Weekly Focus\n\n"
        "## ONE Big Thing\n\n"
        "> **If I accomplish nothing else, I will:**\n"
        "> Старая цель недели\n\n"
        "<!-- This is read by the bot during daily processing -->\n\n"
        "---\n\n"
        "## Week at a Glance\n\n"
        "Всё по плану.\n",
        encoding="utf-8",
    )

    assert dashboard.read_weekly_one_big_thing(vault) == "Старая цель недели"

    dashboard.update_weekly_one_big_thing(vault, "  Выпустить релиз   2.0  ")

    assert dashboard.read_weekly_one_big_thing(vault) == "Выпустить релиз 2.0"
    updated = weekly.read_text(encoding="utf-8")
    assert "Старая цель недели" not in updated
    assert "<!-- This is read by the bot during daily processing -->" in updated
    assert "## Week at a Glance" in updated
    assert "Всё по плану." in updated


@pytest.mark.asyncio
async def test_goal_edit_flow_updates_weekly_file_and_supports_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bot = Bot("12345:" + "a" * 35)
    bot.edit_message_text = AsyncMock()
    bot.send_rich_message = AsyncMock()
    message = Message(
        message_id=42,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Owner"),
    ).as_(bot)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=12345, chat_id=1, user_id=1)
    )
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path=vault)
    )
    dashboard.clear_dashboard_session(1)

    query = SimpleNamespace(
        data="menu:goaledit",
        message=message,
        from_user=message.from_user,
        answer=AsyncMock(),
    )
    await menu_handler.handle_menu_callback(query, bot, state)
    assert await state.get_state() == menu_handler.GoalEditState.waiting_for_text.state

    # Submitting a new goal updates goals/3-weekly.md and clears FSM state.
    reply = message.model_copy(update={"text": "Закрыть аудит безопасности"}).as_(bot)
    await menu_handler.handle_goal_input(reply, state)
    assert await state.get_state() is None
    assert dashboard.read_weekly_one_big_thing(vault) == "Закрыть аудит безопасности"

    # Slash command while waiting raises SkipHandler and clears state.
    await menu_handler.handle_menu_callback(query, bot, state)
    cmd_msg = message.model_copy(update={"text": "/stats"}).as_(bot)
    with pytest.raises(SkipHandler):
        await menu_handler.handle_goal_input(cmd_msg, state)
    assert await state.get_state() is None

    await bot.session.close()
    dashboard.clear_dashboard_session(1)


# --- 6. Deterministic Meeting Prep ----------------------------------------


def test_build_meeting_prep_text_matches_compiled_and_recent_notes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "compiled" / "projects").mkdir(parents=True)
    (vault / "compiled" / "archive").mkdir(parents=True)
    (vault / "daily").mkdir(parents=True)

    (vault / "compiled" / "projects" / "aurora.md").write_text(
        "---\n"
        "type: compiled-briefing\n"
        'description: "Платформа Аврора: миграция БД"\n'
        "---\n"
        "# Аврора\nКонтакт: Алексей Иванов.\n",
        encoding="utf-8",
    )
    (vault / "compiled" / "archive" / "aurora-old.md").write_text(
        '---\ntype: compiled-briefing\ndescription: "Архив Аврора"\n---\n',
        encoding="utf-8",
    )
    day = date(2026, 9, 3)
    (vault / "daily" / f"{(day - timedelta(days=2)).isoformat()}.md").write_text(
        "## 11:00 [text]\nОбсудили с Алексеем сроки по Аврора.\n",
        encoding="utf-8",
    )

    tz = owner_now().tzinfo
    event = CalendarEvent(
        title="Синк по Аврора",
        start=datetime(2026, 9, 3, 14, 0, tzinfo=tz),
        end=datetime(2026, 9, 3, 15, 0, tzinfo=tz),
        all_day=False,
        location="https://meet.example.com/abc",
        attendees=("Алексей Иванов",),
    )

    text = dashboard.build_meeting_prep_text(vault, (event,), day=day)

    assert "Синк по Аврора" in text
    assert "Алексей Иванов" in text
    assert "[[compiled/projects/aurora.md]]" in text
    assert "Платформа Аврора: миграция БД" in text
    assert "aurora-old.md" not in text


@pytest.mark.asyncio
async def test_meeting_prep_callback_handles_unconfigured_and_fetched_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    chat_id = 777003
    dashboard.clear_dashboard_session(chat_id)
    captured: list[str] = []

    async def fake_render_dashboard(bot, *, chat_id, session, text, **kwargs):  # noqa: ANN001, ANN003
        captured.append(text)

    monkeypatch.setattr(dashboard, "render_dashboard", fake_render_dashboard)

    # Unconfigured calendar.
    monkeypatch.setattr(
        menu_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=vault, calendar_ics_url=""),
    )
    query = SimpleNamespace(
        data="menu:meetings",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=60),
        from_user=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )
    await menu_handler.handle_menu_callback(query, object(), AsyncMock())
    assert "CALENDAR_ICS_URL" in captured[-1]

    # Configured calendar with no events.
    monkeypatch.setattr(
        menu_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault, calendar_ics_url="https://cal.example.com/feed.ics"
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "fetch_day",
        lambda url, day: CalendarResult(events=(), error=""),
    )
    query2 = SimpleNamespace(
        data="menu:meetings",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=60),
        from_user=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )
    await menu_handler.handle_menu_callback(query2, object(), AsyncMock())
    assert "встреч в календаре нет" in captured[-1]
    assert owner_today().isoformat() in captured[-1]
