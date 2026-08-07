from datetime import date
from pathlib import Path

import pytest

from d_brain.bot.dashboard import (
    build_brief_type_keyboard,
    build_brief_type_text,
    build_help_text,
    build_home_keyboard,
    build_queue_item_keyboard,
    build_queue_item_text,
    build_queue_keyboard,
    build_queue_text,
    build_stats_text,
    build_weekly_review_keyboard,
    build_weekly_review_text,
    page_fingerprint,
)
from d_brain.services.compiled_enrich_report import WeeklyReview, _ChangeItem
from d_brain.services.decisions_queue import (
    QueueItem,
    format_queue_item_line,
    queue_item_fingerprint,
)
from d_brain.services.file_browser import (
    FileBrowserPathError,
    FileBrowserService,
)
from d_brain.services.session import SessionStore


def test_build_help_text_mentions_menu_files_and_stats() -> None:
    text = build_help_text()

    assert "/menu" in text
    assert "/files" in text
    assert "/stats" in text
    assert "/help" in text
    assert "/status" not in text


def test_help_text_keeps_start_menu_do_at_the_top() -> None:
    text = build_help_text()

    assert text.index("/start") < text.index("/menu") < text.index("/do")


def test_home_keyboard_has_close_button_and_no_help_button() -> None:
    keyboard = build_home_keyboard()
    labels = [
        button.text
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "❌ Закрыть меню" in labels
    assert all("Помощ" not in label for label in labels)


def test_home_keyboard_has_digest_queue_and_brief_buttons() -> None:
    keyboard = build_home_keyboard()
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "menu:digest" in callback_data
    assert "menu:queue" in callback_data
    assert "menu:brief" in callback_data


def test_build_queue_text_reports_empty_queue_as_friendly_not_error() -> None:
    text = build_queue_text(items=[], page=0, total_pages=1, total_items=0)

    assert "пуста" in text.lower()


def test_home_keyboard_has_weekly_review_button() -> None:
    keyboard = build_home_keyboard()
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "menu:weekly" in callback_data


def test_build_queue_text_line_matches_shared_format_queue_item_line() -> None:
    """Invariant (задача N): the bot's "Очередь" screen and the
    human-readable queue file both build each item's line with
    ``format_queue_item_line`` -- this proves the screen actually calls it
    rather than keeping its own copy of the format."""
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/vendor.md",
        summary="старое против нового",
        since="2026-07-01",
    )

    text = build_queue_text(items=[item], page=0, total_pages=1, total_items=1)

    assert format_queue_item_line(item) in text


def test_build_queue_text_shows_known_and_unknown_kind_labels() -> None:
    items = [
        QueueItem(
            kind="fact-check-rejected",
            page="compiled/decisions/vendor.md",
            summary="страница устарела",
            since="2026-07-01",
        ),
        QueueItem(
            kind="drift",
            page="compiled/topics/roadmap.md",
            summary="дрейф темы",
            since="2026-07-02",
        ),
        QueueItem(
            kind="not-a-real-kind",
            page="compiled/topics/backlog.md",
            summary="что-то новое",
            since="2026-07-03",
        ),
    ]

    text = build_queue_text(items=items, page=0, total_pages=1, total_items=3)

    assert "проверка фактов" in text
    assert "дрейф" in text
    assert "неизвестный тип: not-a-real-kind" in text


def test_build_queue_keyboard_encodes_index_not_long_path() -> None:
    long_page = "compiled/decisions/" + ("a" * 80) + ".md"
    items = [
        QueueItem(
            kind="conflict", page=long_page, summary="конфликт", since="2026-07-01"
        )
    ]

    keyboard = build_queue_keyboard(items=items, page=0, total_items=1)
    callback_data = keyboard.inline_keyboard[0][0].callback_data

    assert callback_data == f"menu:queueitem:0:{queue_item_fingerprint(items[0])}"
    assert len(callback_data.encode("utf-8")) <= 64


def test_build_queue_keyboard_shows_pagination_arrows_at_edges() -> None:
    items = [
        QueueItem(kind="conflict", page=f"p{i}.md", summary="s", since="2026-07-01")
        for i in range(3)
    ]

    # QUEUE_PAGE_SIZE is 8, so 17 items span 3 pages (0, 1, 2).
    first_page = build_queue_keyboard(items=items, page=0, total_items=17)
    first_row_data = [button.callback_data for button in first_page.inline_keyboard[-2]]
    assert not any(data.startswith("menu:queuepage:-") for data in first_row_data)
    assert any(data == "menu:queuepage:1" for data in first_row_data)

    last_page = build_queue_keyboard(items=items, page=2, total_items=17)
    last_row_data = [button.callback_data for button in last_page.inline_keyboard[-2]]
    assert not any(data == "menu:queuepage:3" for data in last_row_data)
    assert any(data == "menu:queuepage:1" for data in last_row_data)


def test_queue_refresh_and_back_buttons_keep_the_current_page() -> None:
    """Code review: bare ``menu:queue`` is the *entry point* callback and
    always opens at page 0 -- using it for "Обновить" and "⬅️ К очереди"
    threw an owner working through a later page back to the top. Both
    reuse the existing pagination callback instead."""
    items = [
        QueueItem(kind="conflict", page=f"p{i}.md", summary="s", since="2026-07-01")
        for i in range(3)
    ]

    list_keyboard = build_queue_keyboard(items=items, page=2, total_items=17)
    list_row = list_keyboard.inline_keyboard[-1]
    assert [button.callback_data for button in list_row] == [
        "menu:queuepage:2",
        "menu:home",
    ]

    item_row = build_queue_item_keyboard(
        index=0, item=items[0], page=2
    ).inline_keyboard[-1]
    assert [button.callback_data for button in item_row] == [
        "menu:queuepage:2",
        "menu:home",
    ]


def test_build_queue_item_text_includes_since_when_present_or_absent() -> None:
    with_since = QueueItem(
        kind="conflict", page="p.md", summary="s", since="2026-07-01"
    )
    without_since = QueueItem(kind="conflict", page="p.md", summary="s", since="")

    text_with = build_queue_item_text(with_since)
    text_without = build_queue_item_text(without_since)

    assert "2026-07-01" in text_with
    assert "С какого момента" not in text_without


def test_build_queue_item_keyboard_shows_real_claim_text_for_conflict() -> None:
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/vendor.md",
        summary="s",
        since="2026-07-01",
        conflict_row=(
            "2026-07-01",
            "Поставщик — Acme Corp",
            "daily/2026-06-01.md",
            "Поставщик — Globex LLC",
            "daily/2026-07-01.md",
        ),
    )

    keyboard = build_queue_item_keyboard(index=0, item=item, page=0)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    fingerprint = queue_item_fingerprint(item)

    assert any("Acme Corp" in label for label in labels)
    assert any("Globex LLC" in label for label in labels)
    assert f"menu:queueact:0:{fingerprint}:keep_existing" in callback_data
    assert f"menu:queueact:0:{fingerprint}:keep_new" in callback_data


def test_build_queue_item_keyboard_unknown_kind_only_offers_reject_and_defer() -> None:
    item = QueueItem(kind="drift", page="p.md", summary="s", since="")

    keyboard = build_queue_item_keyboard(index=2, item=item, page=0)
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    fingerprint = queue_item_fingerprint(item)

    assert f"menu:queueact:2:{fingerprint}:reject" in callback_data
    assert f"menu:queueact:2:{fingerprint}:defer" in callback_data
    assert not any(
        data.startswith(f"menu:queueact:2:{fingerprint}:confirm")
        for data in callback_data
    )


def test_build_queue_item_keyboard_callback_data_stays_within_telegram_limit() -> None:
    # Worst case: a 2-digit index and the longest action id, "keep_existing"
    # (задача L code review, defect 4 -- see build_queue_item_keyboard's
    # docstring for the byte-budget breakdown: 14 + 2 + 1 + 8 + 1 + 13 = 39).
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/" + ("a" * 80) + ".md",
        summary="s",
        since="2026-07-01",
        conflict_row=("2026-07-01", "old", "daily/x.md", "new", "daily/y.md"),
    )

    keyboard = build_queue_item_keyboard(index=42, item=item, page=5)
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]

    assert callback_data  # sanity: keyboard actually has action buttons
    for data in callback_data:
        assert data is not None
        assert len(data.encode("utf-8")) <= 64


def test_build_brief_type_text_mentions_query_input() -> None:
    text = build_brief_type_text()

    assert "тип" in text.lower()


def test_build_brief_type_keyboard_offers_all_three_types() -> None:
    keyboard = build_brief_type_keyboard()
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]

    assert "menu:brieftype:decision" in callback_data
    assert "menu:brieftype:topic" in callback_data
    assert "menu:brieftype:project" in callback_data
    assert "menu:home" in callback_data


# --- weekly review (задача N, missed acceptance criterion) -------------------


def _sample_weekly_review(
    *,
    changes: tuple[_ChangeItem, ...] = (),
    queue_items: tuple[QueueItem, ...] = (),
    revisit: tuple[tuple[str, str], ...] = (),
    review_pick: tuple[str, str] | None = None,
) -> WeeklyReview:
    return WeeklyReview(
        start=date(2026, 7, 30),
        end=date(2026, 8, 5),
        queue_items=queue_items,
        changes=changes,
        revisit=revisit,
        review_pick=review_pick,
    )


def test_page_fingerprint_is_stable_8_hex_and_differs_by_path() -> None:
    fingerprint = page_fingerprint("compiled/topics/aurora.md")

    assert fingerprint == page_fingerprint("compiled/topics/aurora.md")
    assert len(fingerprint) == 8
    assert all(character in "0123456789abcdef" for character in fingerprint)
    assert fingerprint != page_fingerprint("compiled/topics/other.md")


def test_build_weekly_review_text_shows_period_changes_and_queue() -> None:
    change = _ChangeItem(
        rel_path="compiled/topics/aurora.md",
        title="Проект Аврора",
        domain="topics",
        created_today=True,
        what_added="обновление",
    )
    queue_item = QueueItem(
        kind="conflict", page="compiled/decisions/vendor.md",
        summary="s", since="2026-08-01",
    )
    review = _sample_weekly_review(
        changes=(change,),
        queue_items=(queue_item,),
        revisit=(("compiled/topics/cold.md", "Забытая страница"),),
        review_pick=("compiled/topics/aurora.md", "Проект Аврора"),
    )

    text = build_weekly_review_text(review)

    assert "2026-07-30" in text
    assert "2026-08-05" in text
    assert "compiled/topics/aurora.md" in text
    assert format_queue_item_line(queue_item) in text
    assert "Забытая страница" in text
    assert "Подтвердить как просмотренную" in text


def test_build_weekly_review_text_no_changes_reports_no_forgotten_page_either() -> None:
    """Known edge case, inherited unchanged (задача N): a week with no
    changes also gets no forgotten-page suggestion or review pick -- the
    screen must say so honestly, not hide the section or claim a pick."""
    review = _sample_weekly_review()

    text = build_weekly_review_text(review)

    assert "Изменений за неделю не было." in text
    assert "предложений на этой неделе нет" in text
    assert "кандидатов на этой неделе нет" in text


def test_build_weekly_review_text_caps_queue_preview_pointing_to_full_screen() -> None:
    items = tuple(
        QueueItem(
            kind="conflict",
            page=f"compiled/topics/p{i}.md",
            summary="s",
            since="2026-08-01",
        )
        for i in range(7)
    )
    review = _sample_weekly_review(queue_items=items)

    text = build_weekly_review_text(review)

    assert "…и ещё 2" in text
    assert "«Очередь»" in text


def test_build_weekly_review_keyboard_confirm_button_carries_page_fingerprint() -> None:
    review = _sample_weekly_review(
        review_pick=("compiled/topics/aurora.md", "Проект Аврора"),
    )

    keyboard = build_weekly_review_keyboard(review)
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]

    fingerprint = page_fingerprint("compiled/topics/aurora.md")
    assert f"menu:weeklyreview:{fingerprint}" in callback_data
    assert "menu:queue" in callback_data
    assert "menu:weekly" in callback_data
    assert "menu:home" in callback_data


def test_build_weekly_review_keyboard_no_confirm_button_when_no_pick() -> None:
    review = _sample_weekly_review(review_pick=None)

    keyboard = build_weekly_review_keyboard(review)
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]

    assert not any(data.startswith("menu:weeklyreview:") for data in callback_data)


def test_build_stats_text_uses_capture_counts_only(tmp_path: Path) -> None:
    session = SessionStore(tmp_path)
    session.append(42, "voice")
    session.append(42, "document")
    session.append(42, "command", cmd="/menu")

    text = build_stats_text(tmp_path, 42)

    assert "🎤 Голосовые: 1" in text
    assert "📄 Документы: 1" in text
    assert "Всего записей: **2**" in text
    assert "command" not in text


def test_file_browser_roots_are_curated_and_presence_based(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "goals").mkdir()

    roots = FileBrowserService(tmp_path).list_roots()
    root_ids = [root.id for root in roots]

    assert root_ids == ["daily", "goals", "vault"]


def test_file_browser_skips_symlinks_and_hidden_top_level_dirs(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-04-04.md").write_text("x", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / ".session").mkdir()
    (tmp_path / "z-file.txt").write_text("z", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "z-file.txt")

    _root, _current_dir, entries = FileBrowserService(tmp_path).list_entries(
        root_id="vault"
    )
    names = [entry.name for entry in entries]

    assert ".session" not in names
    assert "link.txt" not in names
    assert names[:2] == ["daily", "notes"]
    assert names[-1] == "z-file.txt"


def test_file_browser_prevents_path_escape(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    service = FileBrowserService(tmp_path)

    with pytest.raises(FileBrowserPathError):
        service.resolve_path(root_id="daily", relative_path="../outside.txt")


def test_file_browser_rejects_hidden_internal_paths_for_full_vault(
    tmp_path: Path,
) -> None:
    (tmp_path / ".session").mkdir()

    service = FileBrowserService(tmp_path)

    with pytest.raises(FileBrowserPathError):
        service.resolve_path(root_id="vault", relative_path=".session")
