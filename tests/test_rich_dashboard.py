from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from d_brain.bot import dashboard
from d_brain.bot.handlers import brief, do, menu
from d_brain.bot.states import BriefCommandState, DoCommandState


class ButtonsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = []
        self.rows = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tg-button":
            self.buttons.append(dict(attrs))
        if tag == "tg-button-row":
            self.rows += 1


def test_rich_buttons_preserve_payloads_and_escape_labels():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='A < B & "C"', callback_data='menu:test:"&'),
            ]
        ]
    )
    rich = dashboard.build_rich_menu(
        "**Заголовок**\n<tg-button>чужой текст</tg-button>", keyboard
    )
    parser = ButtonsParser()
    parser.feed(rich.html)
    assert parser.rows == 1
    assert parser.buttons == [
        {
            "type": "callback_data",
            "data": 'menu:test:"&',
            "style": "link",
        }
    ]
    assert "<b>Заголовок</b><br>" in rich.html
    assert "&lt;tg-button&gt;" in rich.html
    assert "A &lt; B &amp; &quot;C&quot;" in rich.html


def test_all_previous_actions_are_reachable_and_navigation_is_last():
    home = dashboard.build_home_keyboard()
    sections = [
        dashboard.build_section_keyboard(name)
        for name in ("summaries", "processing", "more")
    ]
    callbacks = {
        b.callback_data
        for k in [home, *sections]
        for row in k.inline_keyboard
        for b in row
    }
    assert {
        "menu:stats",
        "menu:files",
        "menu:process",
        "menu:processfull",
        "menu:do",
        "menu:close",
        "menu:digest",
        "menu:queue",
        "menu:brief",
        "menu:weekly",
        "menu:jobhealth",
        "menu:vaultmap",
    } <= callbacks
    assert len([b for row in home.inline_keyboard for b in row]) == 7
    assert [b.callback_data for b in home.inline_keyboard[0]] == ["menu:do"]
    for keyboard in sections:
        assert [b.callback_data for b in keyboard.inline_keyboard[-1]] == [
            "menu:home",
            "menu:close",
        ]


@pytest.mark.asyncio
async def test_rich_menu_sends_once_and_edits_the_same_message():
    bot = SimpleNamespace(
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        edit_message_text=AsyncMock(),
    )
    session = dashboard.DashboardSession(chat_id=1)
    await dashboard.render_dashboard(
        bot,
        chat_id=1,
        session=session,
        text="Меню",
        keyboard=dashboard.build_home_keyboard(),
    )
    await dashboard.render_dashboard(
        bot,
        chat_id=1,
        session=session,
        text="Сводки",
        keyboard=dashboard.build_section_keyboard("summaries"),
    )
    assert bot.send_rich_message.await_count == 1
    edit = bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == session.dashboard_message_id == 42
    assert edit["reply_markup"] is None
    assert "menu:digest" in edit["rich_message"].html
    assert "text" not in edit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: message is not modified",
        "Bad Request: message to edit not found",
    ],
)
async def test_repeated_tap_and_deleted_menu(description):
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(
                method="editMessageText", message=description
            )
        ),
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=43)),
    )
    session = dashboard.DashboardSession(chat_id=1, dashboard_message_id=42)
    await dashboard.render_dashboard(
        bot,
        chat_id=1,
        session=session,
        text="Меню",
        keyboard=dashboard.build_home_keyboard(),
    )
    if "not modified" in description:
        bot.send_rich_message.assert_not_awaited()
        assert session.dashboard_message_id == 42
    else:
        assert bot.send_rich_message.await_count == 1
        assert session.dashboard_message_id == 43


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, 42])
async def test_rich_rejection_keeps_ordinary_keyboard(monkeypatch, message_id):
    rejection = TelegramBadRequest(
        method="sendRichMessage", message="Unsupported rich message"
    )

    async def edit(**kwargs):
        if "rich_message" in kwargs:
            raise rejection

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=edit),
        send_rich_message=AsyncMock(side_effect=rejection),
    )
    sender = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(dashboard, "send_text", sender)
    session = dashboard.DashboardSession(chat_id=1, dashboard_message_id=message_id)
    keyboard = dashboard.build_home_keyboard()
    await dashboard.render_dashboard(
        bot,
        chat_id=1,
        session=session,
        text="Меню",
        keyboard=keyboard,
    )
    if message_id:
        payload = bot.edit_message_text.await_args.kwargs
        assert payload["reply_markup"] is keyboard
        assert payload["parse_mode"] == "MarkdownV2"
        sender.assert_not_awaited()
    else:
        assert sender.await_args.kwargs["reply_markup"] is keyboard
        assert session.dashboard_message_id == 43


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry,back,expected",
    [
        ("menu:do", "menu:home", DoCommandState.waiting_for_input.state),
        (
            "menu:brieftype:project",
            "menu:brief",
            BriefCommandState.waiting_for_query.state,
        ),
    ],
)
@pytest.mark.parametrize(
    "exit_action", ["back", "menu:home", "menu:close", "command", "submit", "dash"]
)
async def test_input_opens_in_menu_and_exit_clears_waiting(
    monkeypatch,
    tmp_path,
    entry,
    back,
    expected,
    exit_action,
):
    bot = Bot("12345:" + "a" * 35)
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    message = Message(
        message_id=42,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Test"),
    ).as_(bot)
    monkeypatch.setattr(bot.session, "make_request", AsyncMock(return_value=message))
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=12345, chat_id=1, user_id=1)
    )
    monkeypatch.setattr(
        menu, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    monkeypatch.setattr(
        do, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    dashboard.clear_dashboard_session(1)
    query = SimpleNamespace(
        data=entry, message=message, from_user=message.from_user, answer=AsyncMock()
    )
    await menu.handle_menu_callback(query, bot, state)
    assert await state.get_state() == expected
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 42
    assert (
        "tg-button-row" in bot.edit_message_text.await_args.kwargs["rich_message"].html
    )
    bot.send_rich_message.assert_not_awaited()
    if exit_action in {"submit", "dash"}:
        response = message.model_copy(
            update={
                "text": "-" if exit_action == "dash" else "Тестовый вопрос",
            }
        ).as_(bot)
        processor = AsyncMock()
        bot.send_message = AsyncMock()
        if entry == "menu:do":
            monkeypatch.setattr(do, "process_request", processor)
            await do.handle_do_input(response, bot, state)
        else:
            monkeypatch.setattr(brief, "process_brief_request", processor)
            await brief.handle_brief_query(response, state)
        assert processor.await_count == (1 if exit_action == "submit" else 0)
        assert dashboard.get_dashboard_session(1).current_screen not in {
            "do_input",
            "brief_input",
        }
    elif exit_action == "command":
        await menu.cmd_menu(message, bot, state)
    else:
        query.data = back if exit_action == "back" else exit_action
        await menu.handle_menu_callback(query, bot, state)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    await bot.session.close()
    dashboard.clear_dashboard_session(1)


@pytest.mark.asyncio
async def test_navigation_acknowledges_before_rendering(monkeypatch):
    state = AsyncMock()
    query = SimpleNamespace(
        data="menu:summaries",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=42),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    monkeypatch.setattr(
        menu, "get_settings", lambda: SimpleNamespace(vault_path="unused")
    )

    async def render(*args, **kwargs):
        query.answer.assert_awaited_once()

    monkeypatch.setattr(menu, "render_section", render)
    await menu.handle_menu_callback(query, object(), state)
