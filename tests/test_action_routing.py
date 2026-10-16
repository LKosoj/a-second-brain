"""Explicit work requests must reach the existing /do execution path."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from d_brain.bot.handlers import document, text
from d_brain.services.processor import CliProcessor


@pytest.mark.parametrize(
    ("intent", "confidence", "expected"),
    [
        ("action", "high", "action"),
        ("action", "medium", "capture"),
        ("capture", "high", "capture"),
        ("question", "high", "question"),
    ],
)
def test_classifier_accepts_explicit_action(
    tmp_path, monkeypatch, intent, confidence, expected
):
    processor = CliProcessor(tmp_path)
    monkeypatch.setattr(
        processor,
        "_run_json_phase",
        lambda *args, **kwargs: {"intent": intent, "confidence": confidence},
    )
    result = processor.classify_text_intent(
        "Нужно выполнить это: скорректировать презентацию"
    )
    assert result["intent"] == expected


@pytest.mark.parametrize("prefix", ["", "Нужно выполнить это:\n\n"])
async def test_plain_text_action_runs_without_capture(tmp_path, monkeypatch, prefix):
    message = SimpleNamespace(
        text=prefix + "Скорректировать презентацию, содержание и оформление.",
        from_user=SimpleNamespace(id=42),
    )
    monkeypatch.setattr(text, "is_reply_to_bot", lambda message: False)
    monkeypatch.setattr(
        text,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="codex",
            owner_full_name="Example Owner",
            content_language="ru",
        ),
    )
    monkeypatch.setattr(
        text,
        "CliProcessor",
        lambda *args, **kwargs: SimpleNamespace(
            classify_text_intent=lambda value: {
                "intent": "action",
                "confidence": "high",
            },
        ),
    )
    monkeypatch.setattr(text, "SessionStore", lambda path: SimpleNamespace())
    monkeypatch.setattr(text, "_upsert_status_message", AsyncMock(return_value=None))
    execute = AsyncMock()
    monkeypatch.setattr(text, "process_request", execute)

    await text.handle_text(message)

    execute.assert_awaited_once_with(message, message.text, 42)


@pytest.mark.parametrize(
    ("intent", "forwarded", "should_execute"),
    [("action", False, True), ("capture", False, False), ("action", True, False)],
)
async def test_document_caption_action_receives_original_file(
    tmp_path,
    monkeypatch,
    intent,
    forwarded,
    should_execute,
):
    message = SimpleNamespace(
        caption="Скорректировать презентацию",
        from_user=SimpleNamespace(id=42),
        forward_origin=SimpleNamespace() if forwarded else None,
    )
    monkeypatch.setattr(
        document,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            ai_cli="codex",
            content_language="ru",
        ),
    )
    monkeypatch.setattr(
        document,
        "CliProcessor",
        lambda *args, **kwargs: SimpleNamespace(
            classify_text_intent=lambda value: {"intent": intent},
        ),
    )
    execute = AsyncMock()
    monkeypatch.setattr(document, "process_request", execute)
    result = SimpleNamespace(
        original_path="imports/deck.pptx", text_path="imports/deck.txt"
    )

    await document._execute_document_caption(message, result)

    assert execute.await_count == int(should_execute)
    if should_execute:
        args = execute.call_args.args
        assert args[0] is message
        assert message.caption in args[1]
        assert str(tmp_path / Path(result.original_path)) in args[1]
        assert str(tmp_path / Path(result.text_path)) in args[1]
        assert args[2] == 42


@pytest.mark.parametrize(
    ("choice", "score", "expected"),
    [
        ("action", 0.95, "action"),
        ("question", 0.9, "question"),
        ("capture", 0.99, "capture"),
        ("action", 0.7, "action"),
        ("action", 0.76, "action"),
        ("action", 0.79, "action"),
        ("question", 0.76, "question"),
    ],
)
def test_configured_decision_model_classifies_without_cli(
    tmp_path,
    monkeypatch,
    choice,
    score,
    expected,
):
    import httpx

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "answers": {"intent": {"choice": choice, "confidence": score}},
            },
        )

    monkeypatch.setattr("d_brain.services.processor.httpx.post", post)
    processor = CliProcessor(
        tmp_path,
        openai_base_url="https://gateway.example/v1/",
        openai_api_key="test-key",
        classification_model="llmgateway/decision",
        openai_model="chat-fallback",
    )
    monkeypatch.setattr(
        processor,
        "_run_json_phase",
        lambda *args, **kwargs: pytest.fail("Decision classification must not use CLI"),
    )

    result = processor.classify_text_intent("Скорректировать презентацию")

    assert result["intent"] == expected
    url, kwargs = calls[0]
    assert url == "https://gateway.example/v1/decisions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["json"]["model"] == "llmgateway/decision"
    assert kwargs["json"]["state"] == "Скорректировать презентацию"
    assert set(kwargs["json"]["questions"]["intent"]["criteria"]) == {
        "capture",
        "question",
        "action",
    }


def test_empty_classification_model_keeps_cli(tmp_path, monkeypatch):
    processor = CliProcessor(tmp_path, classification_model=" ")
    calls = []

    def run_json(prompt, **kwargs):
        calls.append(kwargs["phase_name"])
        return {"intent": "action", "confidence": "high"}

    monkeypatch.setattr(processor, "_run_json_phase", run_json)
    monkeypatch.setattr(
        processor,
        "_classify_text_with_decision",
        lambda *args: pytest.fail("Empty classification model must keep CLI"),
    )

    assert processor.classify_text_intent("Сделай отчёт")["intent"] == "action"
    assert calls == ["text-intent"]


def test_model_classifies_when_classification_model_empty(tmp_path, monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"intent":"action","confidence":"high"}',
                    )
                )
            ]
        )

    monkeypatch.setattr(
        "d_brain.services.processor.OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        ),
    )
    processor = CliProcessor(tmp_path, openai_api_key="test", openai_model="chat-model")
    monkeypatch.setattr(
        processor,
        "_run_json_phase",
        lambda *args, **kwargs: pytest.fail("MODEL must take priority over CLI"),
    )
    assert processor.classify_text_intent("Сделай отчёт")["intent"] == "action"
    assert calls[0]["model"] == "chat-model"


def test_processor_passes_decision_model_and_cli_to_recall(tmp_path, monkeypatch):
    processor = CliProcessor(
        tmp_path,
        openai_model="chat-model",
        classification_model="decision-model",
    )
    calls = []

    def recall(*args, **kwargs):
        calls.append(kwargs)
        return "memory context"

    monkeypatch.setattr("d_brain.services.processor.build_qmd_recall_block", recall)
    assert processor._build_auto_recall_block("task", purpose="assistant_request") == (
        "memory context"
    )
    assert calls[0]["config"].classification_model == "decision-model"
    assert calls[0]["config"].model == "chat-model"
    assert calls[0]["run_prompt"] == processor._run_vault_prompt


async def test_document_classifies_caption_before_preparing_file(monkeypatch):
    order = []
    edits = []

    async def classify(message):
        order.append("classify")
        return "action"

    def archive(**kwargs):
        order.append("archive")
        raise ValueError("test extraction failure")

    async def edit(message, content, **kwargs):
        edits.append(content)

    monkeypatch.setattr(document, "_classify_document_caption", classify)
    monkeypatch.setattr(document, "edit_text", edit)
    await document._process_document_upload(
        SimpleNamespace(message_id=123, caption="Сделай отчёт"),
        SimpleNamespace(),
        SimpleNamespace(archive_staged_document=archive),
        staged=SimpleNamespace(original_path="deck.pptx", file_format="pptx"),
        file_name="deck.pptx",
        timestamp=None,
        source=None,
    )
    assert order == ["classify", "archive"]
    assert "Поручение принято" in edits[0]


async def test_document_execution_keeps_previously_chosen_route(tmp_path, monkeypatch):
    monkeypatch.setattr(
        document,
        "_classify_document_caption",
        AsyncMock(side_effect=AssertionError("Must not classify a second time")),
    )
    monkeypatch.setattr(
        document, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    execute = AsyncMock()
    monkeypatch.setattr(document, "process_request", execute)
    await document._execute_document_caption(
        SimpleNamespace(caption="Сделай отчёт", from_user=SimpleNamespace(id=42)),
        SimpleNamespace(original_path="deck.pptx", text_path="deck.txt"),
        intent="action",
    )
    execute.assert_awaited_once()
