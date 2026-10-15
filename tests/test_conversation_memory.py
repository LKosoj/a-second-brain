from d_brain.services.session import SessionStore


def test_search_conversation_turns_keeps_one_branch_and_roles(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(7, 11, 10, "Начальный вопрос", "Первый ответ")
    store.save_answer(7, 11, 20, "Нужен целевой факт", "Целевой ответ", parent_id=10)
    store.save_answer(7, 11, 30, "Другая ветка", "Другой ответ", parent_id=10)

    found = store.search_conversation_turns(7, "целевой", before=1, after=2)

    assert found[0]["chat_id"] == 11
    assert found[0]["source_path"] == ".sessions/7/conversations/11.jsonl"
    assert found[0]["matched"] == {
        "message_id": 20,
        "role": "user",
        "text": "Нужен целевой факт",
        "ts": found[0]["matched"]["ts"],
    }
    found_turns = [
        (turn["message_id"], turn["role"], turn["text"])
        for turn in found[0]["turns"]
    ]
    assert found_turns == [
        (10, "user", "Начальный вопрос"),
        (10, "assistant", "Первый ответ"),
        (20, "user", "Нужен целевой факт"),
        (20, "assistant", "Целевой ответ"),
    ]
    assert all(turn["ts"] for turn in found[0]["turns"])


def test_search_conversation_turns_stops_at_fork_and_user_boundary(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(7, 11, 10, "Вопрос", "Искомый ответ")
    store.save_answer(7, 11, 20, "Первая ветка", "Ответ", parent_id=10)
    store.save_answer(7, 11, 30, "Вторая ветка", "Ответ", parent_id=10)
    store.save_answer(8, 11, 10, "Чужой вопрос", "Искомый ответ")

    found = store.search_conversation_turns(7, "ИСКОМЫЙ", after=2)

    assert len(found) == 1
    assert [turn["message_id"] for turn in found[0]["turns"]] == [10, 10]
    assert store.search_conversation_turns(8, "Искомый")
    assert store.search_conversation_turns(9, "Искомый") == []
    assert not (tmp_path / ".sessions/9").exists()


def test_search_conversation_turns_prefers_newest_matches_with_limit(tmp_path) -> None:
    store = SessionStore(tmp_path)
    for message_id in range(1, 5):
        store.save_answer(7, 11, message_id, "Общий вопрос", "Целевой факт")

    found = store.search_conversation_turns(7, "целевой", limit=3)

    assert [item["matched"]["message_id"] for item in found] == [4, 3, 2]


def test_saved_conversation_has_timestamp_and_old_record_still_reads(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_answer(7, 11, 10, "Вопрос", "Ответ")

    entries = store._read_entries(tmp_path / ".sessions/7/conversations/11.jsonl")

    assert entries[0]["ts"]
    assert store.get_conversation(7, 11, 10)[-1] == {
        "role": "assistant",
        "text": "Ответ",
    }
