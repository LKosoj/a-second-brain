from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendRichMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
)

from d_brain.bot.replies import (
    RICH_TEXT_LIMIT,
    answer_rich_text,
    edit_rich_text,
    edit_text,
    send_rich_text,
    send_text,
    text_reply_kwargs,
)


def test_text_reply_kwargs_keeps_kwargs_unchanged_by_default() -> None:
    kwargs = text_reply_kwargs(parse_mode=None)

    assert kwargs["parse_mode"] is None
    assert "reply_markup" not in kwargs


def test_text_reply_kwargs_preserves_explicit_reply_markup() -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open", callback_data="open")]
        ]
    )

    kwargs = text_reply_kwargs(reply_markup=keyboard)

    assert kwargs["reply_markup"] is keyboard


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None]] = []
        self.rich_calls: list[tuple[int, InputRichMessage, dict]] = []
        self.documents: list[tuple[int, object]] = []
        self.rich_error: Exception | None = None

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        self.calls.append((chat_id, text, kwargs.get("parse_mode")))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)

    async def send_document(self, *, chat_id: int, document, **kwargs):
        self.documents.append((chat_id, document))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), document=document)

    async def send_rich_message(self, *, chat_id: int, rich_message, **kwargs):
        self.rich_calls.append((chat_id, rich_message, kwargs))
        if self.rich_error:
            raise self.rich_error
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            rich_message=rich_message,
        )


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, str | None]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.rich_edits: list[tuple[InputRichMessage, dict]] = []
        self.rich_answers: list[tuple[InputRichMessage, dict]] = []
        self.documents: list[object] = []

    async def edit_text(self, text: str | None = None, **kwargs):
        rich_message = kwargs.pop("rich_message", None)
        if rich_message is not None:
            self.rich_edits.append((rich_message, kwargs))
            return SimpleNamespace(rich_message=rich_message)
        assert text is not None
        self.edits.append((text, kwargs.get("parse_mode")))
        return SimpleNamespace(text=text)

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs.get("parse_mode")))
        return SimpleNamespace(text=text)

    async def answer_document(self, document, **kwargs):
        self.documents.append(document)
        return SimpleNamespace(document=document)

    async def answer_rich(self, rich_message, **kwargs):
        self.rich_answers.append((rich_message, kwargs))
        return SimpleNamespace(rich_message=rich_message)


@pytest.mark.asyncio
async def test_send_text_keeps_short_plain_message_inline() -> None:
    bot = _FakeBot()
    text = "x" * 100

    await send_text(bot, chat_id=42, text=text, parse_mode=None)

    assert bot.calls == [(42, text, "MarkdownV2")]
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_text_sends_long_plain_message_as_html_file() -> None:
    bot = _FakeBot()
    text = "x" * 5000

    await send_text(bot, chat_id=42, text=text, parse_mode=None)

    assert bot.calls == []
    assert len(bot.documents) == 1
    _chat_id, document = bot.documents[0]
    assert _chat_id == 42
    assert document.filename == "d-brain-message.html"
    assert text in document.data.decode("utf-8")


@pytest.mark.asyncio
async def test_send_text_converts_legacy_html_to_markdown_v2() -> None:
    bot = _FakeBot()

    await send_text(bot, chat_id=42, text="<b>Готово</b>", parse_mode="HTML")

    assert bot.calls == [(42, "*Готово*", "MarkdownV2")]
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_rich_text_keeps_long_markdown_inline() -> None:
    bot = _FakeBot()
    text = "# Итог\n\n" + "x" * 5000

    await send_rich_text(bot, chat_id=42, text=text)

    assert bot.calls == []
    assert bot.documents == []
    assert len(bot.rich_calls) == 1
    chat_id, rich_message, _kwargs = bot.rich_calls[0]
    assert chat_id == 42
    assert rich_message.markdown == text


@pytest.mark.asyncio
async def test_send_rich_text_preserves_reply_markup() -> None:
    bot = _FakeBot()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open", callback_data="open")]
        ]
    )

    await send_rich_text(
        bot,
        chat_id=42,
        text="**Готово**",
        reply_markup=keyboard,
    )

    assert bot.rich_calls[0][2]["reply_markup"] is keyboard


@pytest.mark.asyncio
async def test_send_rich_text_sends_payload_over_limit_as_html_file() -> None:
    bot = _FakeBot()
    text = "x" * (RICH_TEXT_LIMIT + 1)

    await send_rich_text(bot, chat_id=42, text=text)

    assert bot.rich_calls == []
    assert len(bot.documents) == 1
    assert bot.documents[0][1].filename == "d-brain-message.html"


@pytest.mark.asyncio
async def test_send_rich_text_uses_legacy_delivery_for_media_markup() -> None:
    bot = _FakeBot()
    text = "![](https://example.com/image.jpg)"

    await send_rich_text(bot, chat_id=42, text=text)

    assert bot.rich_calls == []
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_send_rich_text_uses_legacy_delivery_for_map_block() -> None:
    bot = _FakeBot()
    text = '<tg-map lat="41.9" long="12.5" zoom="14"/>'

    await send_rich_text(bot, chat_id=42, text=text)

    assert bot.rich_calls == []
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_send_rich_text_falls_back_once_on_bad_request() -> None:
    bot = _FakeBot()
    rich_message = InputRichMessage(markdown="**Готово**")
    bot.rich_error = TelegramBadRequest(
        SendRichMessage(chat_id=42, rich_message=rich_message),
        "Bad Request: can't parse rich message",
    )

    await send_rich_text(bot, chat_id=42, text="**Готово**")

    assert len(bot.rich_calls) == 1
    assert bot.calls == [(42, "*Готово*", "MarkdownV2")]


@pytest.mark.asyncio
async def test_send_rich_text_does_not_fallback_on_network_error() -> None:
    bot = _FakeBot()
    rich_message = InputRichMessage(markdown="**Готово**")
    bot.rich_error = TelegramNetworkError(
        SendRichMessage(chat_id=42, rich_message=rich_message),
        "connection lost",
    )

    with pytest.raises(TelegramNetworkError):
        await send_rich_text(bot, chat_id=42, text="**Готово**")

    assert len(bot.rich_calls) == 1
    assert bot.calls == []


@pytest.mark.asyncio
async def test_answer_rich_text_uses_message_shortcut() -> None:
    message = _FakeMessage()

    await answer_rich_text(message, "# Итог")

    assert message.answers == []
    assert message.rich_answers[0][0].markdown == "# Итог"


@pytest.mark.asyncio
async def test_edit_rich_text_uses_rich_message_payload() -> None:
    message = _FakeMessage()

    await edit_rich_text(message, "# Итог")

    assert message.edits == []
    assert message.rich_edits[0][0].markdown == "# Итог"


@pytest.mark.asyncio
async def test_edit_text_sends_long_payload_as_html_file() -> None:
    message = _FakeMessage()
    text = "x" * 5000

    await edit_text(message, text, parse_mode=None)

    assert len(message.edits) == 1
    edited_text, edited_mode = message.edits[0]
    assert edited_mode is None
    assert edited_text == "Сообщение слишком длинное, отправляю HTML-файлом."
    assert message.answers == []
    assert len(message.documents) == 1


@pytest.mark.asyncio
async def test_edit_text_uses_markdown_v2_for_short_payload() -> None:
    message = _FakeMessage()

    await edit_text(message, "**Готово**", parse_mode=None)

    assert message.edits == [("*Готово*", "MarkdownV2")]


@pytest.mark.asyncio
async def test_edit_text_preserves_markdown_link_query_params() -> None:
    message = _FakeMessage()

    await edit_text(message, "[x](https://example.com?a=1&b=2)", parse_mode=None)

    assert message.edits == [("[x](https://example.com?a=1&b=2)", "MarkdownV2")]


@pytest.mark.asyncio
async def test_edit_text_preserves_markdown_link_parentheses() -> None:
    message = _FakeMessage()

    await edit_text(message, "[x](https://example.com/a/Foo_(bar))", parse_mode=None)

    assert message.edits == [
        ("[x](https://example.com/a/Foo_(bar\\))", "MarkdownV2")
    ]
