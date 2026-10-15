from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Chat, Document, Message, User, Voice

from d_brain.bot.conversations import is_reply_to_bot, reply_context, save_answer
from d_brain.bot.handlers import do as do_handler
from d_brain.bot.handlers import text as text_handler
from d_brain.bot.handlers import voice as voice_handler
from d_brain.services.processor import CliProcessor
from d_brain.services.session import SessionStore


def message(
    message_id: int,
    text: str | None,
    *,
    reply: Message | None = None,
    sender_id: int = 42,
    is_bot: bool = False,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime(2026, 10, 7, tzinfo=UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=sender_id, is_bot=is_bot, first_name="Example"),
        text=text,
        reply_to_message=reply,
    ).as_(Bot(token="999:" + "a" * 35))


def test_history_survives_restart_and_follows_selected_branch(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(42, 42, 10, "Первый вопрос", "Первый ответ")
    store.save_answer(42, 42, 20, "Другая тема", "Другой ответ")
    store.save_answer(42, 42, 30, "Уточнение", "Ответ на уточнение", parent_id=10)
    store.save_answer(42, 42, 40, "Другая ветка", "Ответ другой ветки", parent_id=10)

    restarted = SessionStore(tmp_path)
    assert restarted.get_conversation(42, 42, 30) == [
        {"role": "user", "text": "Первый вопрос"},
        {"role": "assistant", "text": "Первый ответ"},
        {"role": "user", "text": "Уточнение"},
        {"role": "assistant", "text": "Ответ на уточнение"},
    ]
    assert restarted.get_conversation(42, 42, 10)[-1]["text"] == "Первый ответ"
    assert restarted.get_conversation(43, 42, 30) == []
    assert restarted.get_conversation(42, 43, 30) == []
    assert restarted.get_today(42) == []
    assert restarted.get_recent(42) == []
    assert restarted.get_stats(42) == {}


async def test_reply_context_uses_old_discussion_without_unrelated_daily(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(42, 42, 10, "Вчерашний вопрос", "Вчерашний ответ")
    store.append(42, "text", text="Посторонняя сегодняшняя запись")
    old = message(10, "Вчерашний ответ", sender_id=999, is_bot=True)
    old = old.model_copy(update={"date": datetime(2026, 10, 6, tzinfo=UTC)})
    followup = message(11, "Доработай", reply=old)

    context = await reply_context(followup, tmp_path, 42)

    assert context is not None
    assert "Вчерашний вопрос" in context
    assert "Вчерашний ответ" in context
    assert "Посторонняя" not in context


async def test_only_replies_to_this_bot_select_conversation(tmp_path: Path) -> None:
    assert await reply_context(message(1, "Обычный запрос"), tmp_path, 42) is None
    for sender_id, is_bot in [(42, False), (998, True)]:
        other = message(2, "Другой участник", sender_id=sender_id, is_bot=is_bot)
        followup = message(3, "Ответ", reply=other)
        assert not is_reply_to_bot(followup)
        assert await reply_context(followup, tmp_path, 42) is None
    assert not (tmp_path / ".sessions").exists()


async def test_reply_to_unrecorded_answer_keeps_quote_for_next_turn(
    tmp_path: Path,
) -> None:
    old = message(10, "Ответ до обновления", sender_id=999, is_bot=True)
    followup = message(11, "Продолжи", reply=old)
    assert "Ответ до обновления" in (await reply_context(followup, tmp_path, 42) or "")
    sent = message(12, "Новый ответ", sender_id=999, is_bot=True)
    save_answer(followup, sent, tmp_path, 42, "Продолжи", "Новый ответ")
    assert SessionStore(tmp_path).get_conversation(42, 42, 12) == [
        {"role": "assistant", "text": "Ответ до обновления"},
        {"role": "user", "text": "Продолжи"},
        {"role": "assistant", "text": "Новый ответ"},
    ]


async def test_document_answer_stores_complete_text(tmp_path: Path) -> None:
    incoming = message(1, "Подготовь большой отчёт")
    delivered = message(2, None, sender_id=999, is_bot=True)
    complete = "Полный отчёт " * 1000
    save_answer(incoming, delivered, tmp_path, 42, incoming.text or "", complete)
    followup = message(3, "Сократи", reply=delivered)
    assert complete in (await reply_context(followup, tmp_path, 42) or "")


async def test_old_html_file_is_downloaded_and_kept_for_next_reply(
    tmp_path: Path, monkeypatch
) -> None:
    old = message(10, None, sender_id=999, is_bot=True).model_copy(
        update={
            "document": Document(
                file_id="report", file_unique_id="report", file_name="report.html"
            )
        }
    )
    downloads: list[str] = []

    async def download(bot, file):
        downloads.append(file.file_id)
        return BytesIO(
            '<html><head><style>hidden</style></head><body>'
            '<h1>Анализ проекта</h1><p>Итоги совещаний &amp; решения</p>'
            '</body></html>'.encode()
        )

    monkeypatch.setattr(Bot, "download", download)
    incoming = message(11, "Полный ли анализ?", reply=old)
    context = await reply_context(incoming, tmp_path, 42)
    assert context is not None
    assert "Анализ проекта" in context
    assert "Итоги совещаний & решения" in context
    assert "<html>" not in context
    assert "hidden" not in context
    assert downloads == ["report"]
    assert await reply_context(incoming, tmp_path, 42) == context
    assert downloads == ["report"]
    sent = message(12, "Проверю историю", sender_id=999, is_bot=True)
    save_answer(incoming, sent, tmp_path, 42, "Полный ли анализ?", "Проверю историю")
    next_context = await reply_context(
        message(13, "Продолжай", reply=sent), tmp_path, 42
    )
    assert next_context is not None
    assert "Анализ проекта" in next_context
    assert "Полный ли анализ?" in next_context
    assert "Проверю историю" in next_context
    assert downloads == ["report"]


async def test_saved_history_does_not_download_document(
    tmp_path: Path, monkeypatch
) -> None:
    old = message(10, None, sender_id=999, is_bot=True).model_copy(
        update={
            "document": Document(
                file_id="report", file_unique_id="report", file_name="report.html"
            )
        }
    )
    SessionStore(tmp_path).save_answer(42, 42, 10, "Исходный вопрос", "Полный ответ")

    async def download(bot, file):
        pytest.fail("Stored history should be used without downloading")

    monkeypatch.setattr(Bot, "download", download)
    context = await reply_context(message(11, "Продолжай", reply=old), tmp_path, 42)
    assert context is not None
    assert "Исходный вопрос" in context
    assert "Полный ответ" in context


async def test_unreadable_document_does_not_supply_empty_context(
    tmp_path: Path, monkeypatch
) -> None:
    old = message(10, None, sender_id=999, is_bot=True).model_copy(
        update={
            "document": Document(
                file_id="report", file_unique_id="report", file_name="report.html"
            )
        }
    )

    async def download(bot, file):
        return BytesIO(b"<html><body></body></html>")

    monkeypatch.setattr(Bot, "download", download)
    with pytest.raises(ValueError, match="не удалось прочитать текст"):
        await reply_context(message(11, "Продолжай", reply=old), tmp_path, 42)
    assert SessionStore(tmp_path).get_conversation(42, 42, 10) == []


async def test_text_reply_bypasses_capture_classifier(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    async def process(incoming: Message, prompt: str, user_id: int) -> None:
        calls.append((prompt, user_id))

    def unexpected_settings() -> None:
        pytest.fail("Reply must bypass the ordinary text classifier")

    monkeypatch.setattr(text_handler, "process_request", process)
    monkeypatch.setattr(text_handler, "get_settings", unexpected_settings)
    old = message(10, "Варианты решения", sender_id=999, is_bot=True)
    await text_handler.handle_text(message(11, "Доработай второй", reply=old))
    assert calls == [("Доработай второй", 42)]


async def test_voice_reply_continues_without_capture(
    tmp_path: Path, monkeypatch
) -> None:
    old = message(10, "Варианты решения", sender_id=999, is_bot=True)
    incoming = message(11, None, reply=old).model_copy(
        update={"voice": Voice(file_id="audio", file_unique_id="audio", duration=1)}
    )
    calls: list[str] = []

    class Transcriber:
        def __init__(self, *args) -> None:
            pass

        async def transcribe(self, audio: bytes) -> str:
            return "Доработай второй"

    async def get_file(file_id: str):
        return SimpleNamespace(file_path="audio.ogg")

    async def download_file(path: str):
        return BytesIO(b"audio")

    async def typing(*args, **kwargs) -> None:
        pass

    async def process(incoming: Message, prompt: str, user_id: int) -> None:
        calls.append(prompt)

    monkeypatch.setattr(Chat, "do", typing)
    monkeypatch.setattr(voice_handler, "DeepgramTranscriber", Transcriber)
    monkeypatch.setattr(voice_handler, "process_request", process)
    monkeypatch.setattr(
        voice_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path, content_language="ru", deepgram_api_key=""
        ),
    )
    await voice_handler.handle_voice(
        incoming, SimpleNamespace(get_file=get_file, download_file=download_file)
    )
    assert calls == ["Доработай второй"]
    assert not (tmp_path / "daily").exists()
    assert not (tmp_path / ".sessions").exists()


@pytest.mark.parametrize("route", ["question", "do"])
async def test_reply_to_last_separate_file_restores_whole_discussion(
    tmp_path: Path, monkeypatch, route: str
) -> None:
    handler = text_handler if route == "question" else do_handler
    paths = [str(tmp_path / "first.md"), str(tmp_path / "last.md")]
    old = message(10, "Предыдущий ответ", sender_id=999, is_bot=True)
    SessionStore(tmp_path).save_answer(
        42, 42, 10, "Исходный вопрос", "Предыдущий ответ"
    )
    incoming = message(11, "Подготовь анализ", reply=old if route == "do" else None)
    delivered: list[Message] = []

    class Processor:
        def __init__(self, *args) -> None:
            pass

        def classify_text_intent(self, text: str):
            return {"intent": "question"}

        def answer_question(self, question: str, user_id: int):
            return {"report": "Полный анализ", "artifact_paths": paths}

        def execute_prompt(self, prompt: str, user_id: int, **kwargs):
            return self.answer_question(prompt, user_id)

    async def answer(incoming: Message, text: str) -> Message:
        return message(20, text, sender_id=999, is_bot=True)

    async def status(*args):
        async def delete() -> None:
            pass

        return SimpleNamespace(delete=delete)

    async def edit(*args) -> None:
        pass

    async def document(incoming: Message, file, **kwargs) -> Message:
        sent = message(21 + len(delivered), None, sender_id=999, is_bot=True)
        delivered.append(sent)
        return sent

    monkeypatch.setattr(handler, "CliProcessor", Processor)
    monkeypatch.setattr(handler, "answer_rich_text", answer)
    monkeypatch.setattr(handler, "answer_text", status)
    monkeypatch.setattr(handler, "edit_text", edit)
    monkeypatch.setattr(Message, "answer_document", document)
    monkeypatch.setattr(
        handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="claude",
            owner_full_name="Example",
            content_language="ru",
        ),
    )
    if route == "question":
        await handler.handle_text(incoming)
    else:
        await handler.process_request(incoming, incoming.text, 42)

    assert len(delivered) == 2
    followup = message(23, "Все совещания учтены?", reply=delivered[-1])
    context = await reply_context(followup, tmp_path, 42)
    assert context is not None
    assert "Подготовь анализ" in context
    assert "Полный анализ" in context
    assert ("Исходный вопрос" in context) is (route == "do")
    store = SessionStore(tmp_path)
    assert store.get_conversation(42, 42, 20) == store.get_conversation(42, 42, 22)


@pytest.mark.parametrize("continuation", [False, True])
async def test_do_saves_answer_and_passes_context_only_for_reply(
    tmp_path: Path, monkeypatch, continuation: bool
) -> None:
    old = message(10, "Первый ответ", sender_id=999, is_bot=True)
    SessionStore(tmp_path).save_answer(42, 42, 10, "Первый вопрос", "Первый ответ")
    incoming = message(11, "Запрос", reply=old if continuation else None)
    captured: dict = {}

    class Processor:
        def __init__(self, *args) -> None:
            pass

        def execute_prompt(self, prompt, user_id, **kwargs):
            captured.update(kwargs)
            return {"report": "Результат", "processed_entries": 1}

    async def answer(incoming: Message, text: str) -> Message:
        return message(12, text, sender_id=999, is_bot=True)

    async def status(incoming: Message, text: str):
        async def delete() -> None:
            pass

        return SimpleNamespace(delete=delete)

    monkeypatch.setattr(do_handler, "CliProcessor", Processor)
    monkeypatch.setattr(do_handler, "answer_rich_text", answer)
    monkeypatch.setattr(do_handler, "answer_text", status)
    monkeypatch.setattr(
        do_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="claude",
            owner_full_name="Example",
            content_language="ru",
        ),
    )
    await do_handler.process_request(incoming, "Запрос", 42)
    history = SessionStore(tmp_path).get_conversation(42, 42, 12)
    assert history[-2:] == [
        {"role": "user", "text": "Запрос"},
        {"role": "assistant", "text": "Результат"},
    ]
    if continuation:
        assert "Первый вопрос" in captured["conversation_context"]
        assert len(history) == 4
    else:
        assert captured == {}
        assert len(history) == 2


@pytest.mark.parametrize("context", [None, "Selected discussion"])
def test_processor_uses_selected_history_instead_of_daily(
    tmp_path: Path, monkeypatch, context: str | None
) -> None:
    processor = CliProcessor(tmp_path)
    monkeypatch.setattr(processor, "_build_injected_context", lambda **kw: "core")
    monkeypatch.setattr(processor, "_load_todoist_reference", lambda: "todoist")
    monkeypatch.setattr(processor, "_load_vault_retrieval_skill", lambda: "retrieval")
    monkeypatch.setattr(processor, "_get_session_context", lambda user: "Daily history")
    monkeypatch.setattr(processor, "_build_auto_recall_block", lambda *a, **kw: "")
    monkeypatch.setattr(processor, "_file_output_artifact_if_useful", lambda **kw: None)
    prompts: list[str] = []

    def run(prompt: str):
        prompts.append(prompt)
        return "Готово", []

    monkeypatch.setattr(processor, "_run_assistant_prompt_with_artifacts", run)
    assert "error" not in processor.execute_prompt(
        "Следующий шаг", 42, conversation_context=context
    )
    assert ("Daily history" in prompts[0]) is (context is None)
    assert ("Selected discussion" in prompts[0]) is (context is not None)
