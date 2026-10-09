from datetime import date
from pathlib import Path

from conftest import _setup_daily_processing_vault

from d_brain.services.compiled_briefings import (
    CompiledBriefingCandidate,
    CompiledBriefingService,
)
from d_brain.services.daily_workflow import SCHEDULED_MODE
from d_brain.services.processor import CliProcessor
from d_brain.services.session import SessionStore


def test_original_conversations_are_requested_and_user_scoped(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(7, 11, 10, "Обсудим бюджет проекта", "Бюджет 100")
    store.save_answer(8, 11, 10, "Обсудим бюджет проекта", "Чужой бюджет")
    processor = CliProcessor(tmp_path)

    block = processor._build_conversation_memory_block("Что обсуждали про бюджет?", 7)

    assert "Бюджет 100" in block
    assert "Чужой бюджет" not in block
    assert "telegram:11:10" in block
    assert "Бюджет 100" in processor._build_conversation_memory_block(
        "Что мы решили про бюджет?", 7
    )
    assert processor._build_conversation_memory_block("Какой бюджет?", 7) == ""
    assert (
        processor._build_conversation_memory_block("Что обсуждали про бюджет?", 0) == ""
    )


def test_original_conversations_prefer_specific_old_topic_over_new_budget_threads(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(7, 11, 10, "Бюджет проекта Орион", "Орион — 400")
    for message_id, project in ((20, "Вега"), (30, "Альтаир"), (40, "Дельта")):
        store.save_answer(7, 11, message_id, f"Бюджет {project}", project)
    processor = CliProcessor(tmp_path)

    block = processor._build_conversation_memory_block(
        "Что мы решили про бюджет проекта Орион?", 7
    )

    assert "Орион — 400" in block
    assert "Дельта" not in block


def test_empty_daily_still_learns_before_refresh(tmp_path: Path, monkeypatch) -> None:
    day = date(2026, 10, 9)
    vault_path = tmp_path / "vault"
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path, owner_telegram_id=7)
    events: list[str] = []
    monkeypatch.setattr(processor, "_daily_has_processable_entries", lambda _: False)
    monkeypatch.setattr(processor, "_log_graph_age_warning", lambda: None)
    monkeypatch.setattr(processor, "_rebuild_graph", lambda: None)
    monkeypatch.setattr(
        processor, "_learn_from_conversations", lambda _: events.append("learn")
    )
    monkeypatch.setattr(
        processor, "_refresh_qmd_index", lambda: events.append("refresh")
    )

    result = processor.process_daily(day, mode=SCHEDULED_MODE)

    assert result["empty_daily"] is True
    assert events == ["learn", "refresh"]


def test_conditional_lessons_reach_question_and_do(tmp_path: Path, monkeypatch) -> None:
    day = date.today()
    vault_path = tmp_path / "vault"
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    marker = "Apply only when the budget condition matches."
    monkeypatch.setattr(processor, "_build_conditional_lessons_block", lambda _: marker)
    monkeypatch.setattr(
        processor, "_build_auto_recall_block", lambda *args, **kwargs: ""
    )
    monkeypatch.setattr(processor, "_build_compiled_briefings_block", lambda _: "")
    monkeypatch.setattr(
        processor, "_file_output_artifact_if_useful", lambda **kwargs: None
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        processor,
        "_run_assistant_prompt_with_artifacts",
        lambda prompt: (prompts.append(prompt) or "Ответ", []),
    )

    prompt = processor._inject_question_context_blocks(
        "USER QUESTION:\nБюджет?", "Бюджет?"
    )
    result = processor.execute_prompt("Бюджет?")

    assert marker in prompt
    assert marker in prompts[0]
    assert "error" not in result


def test_nightly_correction_is_applied_with_original_source(
    tmp_path: Path, monkeypatch
) -> None:
    day = date.today()
    vault_path = tmp_path / "vault"
    _setup_daily_processing_vault(vault_path, day)
    store = SessionStore(vault_path)
    store.save_answer(7, 11, 10, "Как считать бюджет?", "В евро")
    store.save_answer(
        7, 11, 20, "Нет, бюджет нужно считать в рублях", "Учту", parent_id=10
    )
    archive = vault_path / ".sessions/7/conversations/11.jsonl"
    original = archive.read_bytes()
    processor = CliProcessor(vault_path, owner_telegram_id=7)
    monkeypatch.setattr(
        processor,
        "_run_json_phase",
        lambda *args, **kwargs: {
            "correction_lessons": [
                {
                    "condition": "При расчёте бюджет проекта",
                    "lesson": "Бюджет считать в рублях",
                    "source": {
                        "kind": "explicit_user_correction",
                        "chat_id": 11,
                        "message_ids": [10, 20],
                        "quote": "Нет, бюджет нужно считать в рублях",
                    },
                }
            ],
        },
    )

    written = processor._learn_from_conversations(day)
    block = processor._build_conditional_lessons_block("Уточни бюджет проекта")

    assert len(written) == 1
    assert "Бюджет считать в рублях" in block
    assert "telegram:11:20" in block
    assert archive.read_bytes() == original
    assert processor._learn_from_conversations(day) == []


def test_stale_source_snapshot_reaches_answer_provenance(tmp_path: Path, monkeypatch):
    vault_path = tmp_path / "vault"
    _setup_daily_processing_vault(vault_path, date.today())
    source = vault_path / "daily/new-evidence.md"
    source.write_text("Updated budget is 200.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/budget.md",
        domain="projects",
        slug="budget",
        title="Budget",
        description="",
        freshness_state="stale",
        confidence="high",
        relevance=1,
        tier="active",
        text="## Sources\n- [[daily/new-evidence.md]]\n",
    )
    monkeypatch.setattr(
        CompiledBriefingService,
        "_rank_candidates",
        lambda *args, **kwargs: [candidate],
    )
    processor = CliProcessor(vault_path)
    touched: list[str] = []
    monkeypatch.setattr(
        processor, "_touch_memory_paths", lambda *paths: touched.extend(paths)
    )

    block = processor._build_compiled_briefings_block("Budget?")
    source.unlink()
    answer = processor._append_question_provenance("Budget answer.", "Budget?")

    assert "Updated budget is 200." in block
    assert "[[daily/new-evidence.md]]" in answer
    assert touched == ["compiled/projects/budget.md"]
