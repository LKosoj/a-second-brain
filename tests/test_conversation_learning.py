import json
from datetime import UTC, date, datetime

from conftest import _write_vault_manifest

from d_brain.services.conversation_learning import ConversationLearningService
from d_brain.services.session import SessionStore

_USER_CORRECTION = "Нет, для этого импорта нужен другой ключ. Проверил — сработало."


def _candidate_vault(
    tmp_path,
    *,
    user_question: str = _USER_CORRECTION,
):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    _write_vault_manifest(vault)
    store = SessionStore(vault)
    store.save_answer(7, 11, 10, "Как проверить импорт?", "Запусти точную проверку.")
    store.save_answer(
        7,
        11,
        20,
        user_question,
        "Исправляю шаг.",
        parent_id=10,
    )
    path = vault / ".sessions/7/conversations/11.jsonl"
    entries = store._read_entries(path)
    entries[1]["ts"] = datetime(2026, 10, 9, tzinfo=UTC).isoformat()
    path.write_text("\n".join(json.dumps(item) for item in entries) + "\n")
    return vault


def test_bounded_candidates_are_owner_scoped_and_require_reply_link(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)

    candidates = service.bounded_candidates(date(2026, 10, 9), 7)

    assert len(candidates) == 1
    assert candidates[0]["source_path"] == ".sessions/7/conversations/11.jsonl"
    assert candidates[0]["parent_id"] == 10
    assert service.bounded_candidates(date(2026, 10, 9), 8) == []


def test_save_lessons_requires_exact_user_quote_and_parent_source(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется этот импорт",
                "lesson": "Использовать другой ключ",
                "exceptions": ["Не применять к другому типу импорта"],
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            }
        ],
        "confirmed_practices": [
            {
                "condition": "Когда нужен тот же импорт",
                "practice": "Проверить другой ключ",
                "outcome": "Импорт сработал",
                "evidence": {
                    "kind": "explicit_user_confirmation",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            }
        ],
    }

    paths = service.save_lessons(date(2026, 10, 9), candidates, result)

    assert paths == [
        "thoughts/learnings/2026-10-09-user-correction-11-20.md",
        "thoughts/learnings/2026-10-09-confirmed-practice-11-20.md",
    ]
    correction = (vault / paths[0]).read_text(encoding="utf-8")
    assert "telegram:11:20" in correction
    assert ".sessions/7/conversations/11.jsonl" in correction
    block = service.relevant_lessons("Как проверить импорт с ключом?")
    assert "Не применять к другому типу импорта" in block
    assert "Apply an item only" in block
    assert "Подтверждённый результат" not in block
    assert "Проверить другой ключ" in block


def test_save_lessons_rejects_assistant_only_or_unmatched_evidence(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [],
        "confirmed_practices": [
            {
                "condition": "Когда нужен импорт",
                "practice": "Запустить команду",
                "outcome": "Готово",
                "evidence": {
                    "kind": "explicit_user_confirmation",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": "Исправляю шаг.",
                },
            }
        ],
    }

    assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_save_lessons_deduplicates_same_condition_and_lesson(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется этот импорт",
                "lesson": "Использовать другой ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            },
            {
                "condition": "когда проверяется этот импорт",
                "lesson": "использовать другой ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            },
        ]
    }

    first = service.save_lessons(date(2026, 10, 9), candidates, result)

    assert len(first) == 1
    assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_save_lessons_deduplicates_across_days_and_rejects_ancestor_quote(
    tmp_path,
) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    duplicate = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется этот импорт",
                "lesson": "Использовать другой ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            }
        ]
    }
    assert service.save_lessons(date(2026, 10, 9), candidates, duplicate)
    assert service.save_lessons(date(2026, 10, 10), candidates, duplicate) == []

    fake_source = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется этот импорт",
                "lesson": "Использовать иной ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": "Как проверить импорт?",
                },
            }
        ]
    }
    assert service.save_lessons(date(2026, 10, 9), candidates, fake_source) == []


def test_correction_cue_does_not_match_word_fragment(tmp_path) -> None:
    quote = "В интернете есть пример."
    vault = _candidate_vault(tmp_path, user_question=quote)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется импорт",
                "lesson": "Использовать иной ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": quote,
                },
            }
        ]
    }

    assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_correction_rejects_affirmation_and_labeled_external_quote(tmp_path) -> None:
    for index, quote in enumerate(
        ["Всё правильно, спасибо.", "> Forwarded: Нет, нужен другой ключ."]
    ):
        vault = _candidate_vault(tmp_path / str(index), user_question=quote)
        service = ConversationLearningService(vault)
        candidates = service.bounded_candidates(date(2026, 10, 9), 7)
        result = {
            "correction_lessons": [
                {
                    "condition": "Когда проверяется импорт",
                    "lesson": "Использовать иной ключ",
                    "source": {
                        "kind": "explicit_user_correction",
                        "chat_id": 11,
                        "message_ids": [10, 20],
                        "quote": quote,
                    },
                }
            ]
        }

        assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_save_lessons_rejects_non_string_required_fields(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": {"when": "import"},
                "lesson": ["Использовать другой ключ"],
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            }
        ],
        "confirmed_practices": [
            {
                "condition": "Когда нужен импорт",
                "practice": ["Проверить другой ключ"],
                "outcome": {"result": "сработало"},
                "evidence": {
                    "kind": "explicit_user_confirmation",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": _USER_CORRECTION,
                },
            }
        ],
    }

    assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_confirmed_practice_rejects_negative_or_failed_tool_output(tmp_path) -> None:
    cases = [
        ("Проверил: не сработало.", "explicit_user_confirmation", "сработало"),
        ("Ещё не готово.", "explicit_user_confirmation", "готово"),
        ("pytest: 1 failed", "user_supplied_tool_evidence", "pytest"),
        ("Проверил — получил ошибку.", "explicit_user_confirmation", "Проверил"),
    ]
    for index, (user_question, kind, _quote) in enumerate(cases):
        vault = _candidate_vault(tmp_path / str(index), user_question=user_question)
        service = ConversationLearningService(vault)
        candidates = service.bounded_candidates(date(2026, 10, 9), 7)
        result = {
            "confirmed_practices": [
                {
                    "condition": "Когда проверяется импорт",
                    "practice": "Проверить ключ",
                    "outcome": "Импорт завершён",
                    "evidence": {
                        "kind": kind,
                        "chat_id": 11,
                        "message_ids": [10, 20],
                        "quote": user_question,
                    },
                }
            ]
        }

        assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_save_lessons_rejects_source_with_unrelated_message_id(tmp_path) -> None:
    vault = _candidate_vault(tmp_path)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": "Когда проверяется этот импорт",
                "lesson": "Использовать другой ключ",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20, 30],
                    "quote": _USER_CORRECTION,
                },
            }
        ]
    }

    assert service.save_lessons(date(2026, 10, 9), candidates, result) == []


def test_save_lessons_accepts_exact_quote_from_current_user_text(tmp_path) -> None:
    user_question = "Нет, бюджет в рублях. Остальное не меняй."
    vault = _candidate_vault(tmp_path, user_question=user_question)
    service = ConversationLearningService(vault)
    candidates = service.bounded_candidates(date(2026, 10, 9), 7)
    result = {
        "correction_lessons": [
            {
                "condition": "Когда уточняется бюджет",
                "lesson": "Указывать бюджет в рублях",
                "source": {
                    "kind": "explicit_user_correction",
                    "chat_id": 11,
                    "message_ids": [10, 20],
                    "quote": "Нет, бюджет в рублях.",
                },
            }
        ]
    }

    assert service.save_lessons(date(2026, 10, 9), candidates, result) == [
        "thoughts/learnings/2026-10-09-user-correction-11-20.md"
    ]


def test_relevant_lessons_prefers_newest_card_when_scores_match(tmp_path) -> None:
    vault = tmp_path / "vault"
    learnings = vault / "thoughts/learnings"
    learnings.mkdir(parents=True)
    cards = (
        ("2026-10-08", "Старое правило"),
        ("2026-10-09", "Новое правило"),
    )
    for day, lesson in cards:
        (learnings / f"{day}-user-correction-11-20.md").write_text(
            "---\n"
            'condition: "Проверка импорта"\n'
            f'lesson: "{lesson}"\n'
            'source: "telegram:11:20"\n'
            "---\n",
            encoding="utf-8",
        )

    block = ConversationLearningService(vault).relevant_lessons(
        "Проверка импорта", limit=1
    )

    assert "Новое правило" in block
    assert "Старое правило" not in block
