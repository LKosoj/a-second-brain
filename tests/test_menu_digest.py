"""Handler-level tests for dashboard callbacks handled directly in
``bot/handlers/menu.py`` -- the "Дайджест" button (задача L, ТЗ 7.1) and the
decisions-queue action buttons' fingerprint check (задача L code review,
defect 4) -- same FakeMessage/monkeypatch approach as
``tests/test_do_cancel.py``'s ``process_request`` tests."""

import json
import shutil
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from d_brain.bot.dashboard import (
    get_dashboard_session,
    page_fingerprint,
    render_file_directory,
)
from d_brain.bot.handlers import menu as menu_handler
from d_brain.services.compiled_enrich_report import WeeklyReview
from d_brain.services.decisions_queue import (
    QueueItem,
    ResponseOutcome,
    queue_item_fingerprint,
)


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


class FakeStatusMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.edits.append((text, kwargs))


class FakeCallbackQuery:
    """Minimal double for aiogram's ``CallbackQuery`` -- same shape as
    ``tests/test_why_handler.py``'s, plus ``message.chat.id``/``message_id``
    since ``handle_menu_callback`` reads those directly."""

    def __init__(self, data: str, chat_id: int, message_id: int) -> None:
        self.data = data
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=message_id
        )
        self.from_user = SimpleNamespace(id=chat_id)
        self.answer_calls: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answer_calls.append((text, show_alert))


@contextmanager
def _fake_vault_write_lock(vault_path):  # noqa: ANN001
    yield SimpleNamespace()


def _stub_answer_text(monkeypatch, status: FakeStatusMessage) -> None:
    async def fake_answer_text(message, text: str, **kwargs) -> object:  # noqa: ANN001, ANN003
        return status

    monkeypatch.setattr(menu_handler, "answer_text", fake_answer_text)


async def test_deliver_daily_digest_reports_no_work_as_friendly_not_error(
    monkeypatch, tmp_path: Path
) -> None:
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler, "build_daily_digest", lambda vault_path, day, *, pass_status: None
    )

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append((text, kwargs))

    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert status.edits == [("Сегодня без изменений — дайджест не нужен.", {})]


async def test_deliver_daily_digest_writes_exactly_like_cli_and_delivers(
    monkeypatch, tmp_path: Path
) -> None:
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler,
        "build_daily_digest",
        lambda vault_path, day, *, pass_status: "**Дайджест обогащения**",
    )
    monkeypatch.setattr(
        menu_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(menu_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        menu_handler, "digest_path", lambda vault_path, day: "summaries/compile/x.md"
    )
    written: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        menu_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: written.append((path, content)),
    )
    monkeypatch.setattr(
        menu_handler, "render_digest_note", lambda day, digest: b"rendered"
    )
    delivered: list[str] = []

    async def fake_edit_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(menu_handler, "edit_rich_text", fake_edit_rich_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert written == [("summaries/compile/x.md", b"rendered")]
    assert delivered == ["**Дайджест обогащения**"]


async def test_deliver_daily_digest_falls_back_to_a_new_message_on_edit_refusal(
    monkeypatch, tmp_path: Path
) -> None:
    """Code review: the ``TelegramBadRequest`` fallback was uncovered.
    Telegram refuses an edit whose text is byte-identical to the message
    already on screen -- exactly what happens when the owner presses
    "Дайджест" twice on a day nothing changed -- so without this branch the
    second press would silently show nothing."""
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler,
        "build_daily_digest",
        lambda vault_path, day, *, pass_status: "**Дайджест обогащения**",
    )
    monkeypatch.setattr(
        menu_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(menu_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        menu_handler, "digest_path", lambda vault_path, day: "summaries/compile/x.md"
    )
    monkeypatch.setattr(
        menu_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    monkeypatch.setattr(
        menu_handler, "render_digest_note", lambda day, digest: b"rendered"
    )

    async def refusing_edit(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        raise TelegramBadRequest(
            method=SimpleNamespace(), message="message is not modified"
        )

    answered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        answered.append(text)

    monkeypatch.setattr(menu_handler, "edit_rich_text", refusing_edit)
    monkeypatch.setattr(menu_handler, "answer_rich_text", fake_answer_rich_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert answered == ["**Дайджест обогащения**"]


async def test_deliver_daily_digest_delivery_crash_still_reaches_the_owner(
    monkeypatch, tmp_path: Path
) -> None:
    """A delivery failure that is not ``TelegramBadRequest`` (a network blip,
    a rate limit) used to be logged and nothing else -- the digest was built
    and filed, but the owner was left staring at "⏳ Строю дайджест..."
    forever, with no way to tell a hung build from a delivered one. The
    digest must still be sent as a new message."""
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler,
        "build_daily_digest",
        lambda vault_path, day, *, pass_status: "**Дайджест обогащения**",
    )
    monkeypatch.setattr(
        menu_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(menu_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        menu_handler, "digest_path", lambda vault_path, day: "summaries/compile/x.md"
    )
    monkeypatch.setattr(
        menu_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    monkeypatch.setattr(
        menu_handler, "render_digest_note", lambda day, digest: b"rendered"
    )

    async def crashing_edit(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        raise RuntimeError("network blip")

    answered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        answered.append(text)

    monkeypatch.setattr(menu_handler, "edit_rich_text", crashing_edit)
    monkeypatch.setattr(menu_handler, "answer_rich_text", fake_answer_rich_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert answered == ["**Дайджест обогащения**"]


async def test_deliver_daily_digest_says_where_the_digest_is_when_chat_is_dead(
    monkeypatch, tmp_path: Path
) -> None:
    """Last resort: both the edit and the new message failed. The spinner
    must still be replaced -- the digest is on disk and the owner needs to
    know that, rather than being told nothing at all."""
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler,
        "build_daily_digest",
        lambda vault_path, day, *, pass_status: "**Дайджест обогащения**",
    )
    monkeypatch.setattr(
        menu_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(menu_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        menu_handler, "digest_path", lambda vault_path, day: "summaries/compile/x.md"
    )
    monkeypatch.setattr(
        menu_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    monkeypatch.setattr(
        menu_handler, "render_digest_note", lambda day, digest: b"rendered"
    )

    async def crashing_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        raise RuntimeError("network blip")

    monkeypatch.setattr(menu_handler, "edit_rich_text", crashing_rich_text)
    monkeypatch.setattr(menu_handler, "answer_rich_text", crashing_rich_text)

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append((text, kwargs))

    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert status.edits == [
        ("❌ Дайджест сохранён в файл, но отправить его в чат не удалось.", {})
    ]


async def test_deliver_daily_digest_write_failure_reports_error_not_silent(
    monkeypatch, tmp_path: Path
) -> None:
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    monkeypatch.setattr(
        menu_handler,
        "build_daily_digest",
        lambda vault_path, day, *, pass_status: "**Дайджест обогащения**",
    )
    monkeypatch.setattr(
        menu_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(menu_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        menu_handler, "digest_path", lambda vault_path, day: "summaries/compile/x.md"
    )

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(menu_handler, "write_validated_vault_markdown", _raise)
    monkeypatch.setattr(
        menu_handler, "render_digest_note", lambda day, digest: b"rendered"
    )

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append((text, kwargs))

    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert status.edits == [
        ("❌ Дайджест построен, но не удалось сохранить файл.", {})
    ]


async def test_deliver_daily_digest_build_failure_reports_error_not_silent(
    monkeypatch, tmp_path: Path
) -> None:
    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("boom")

    monkeypatch.setattr(menu_handler, "build_daily_digest", _raise)

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append((text, kwargs))

    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert status.edits == [("❌ Не удалось построить дайджест.", {})]


# -- defect 5: hardcoded pass status hid the "quiet day" digest suppression --


def test_read_enrich_pass_status_missing_journal_falls_back_to_success(
    tmp_path: Path,
) -> None:
    status = menu_handler._read_enrich_pass_status(tmp_path)

    assert status.status == "success"
    assert status.error == ""


def test_read_enrich_pass_status_malformed_json_is_unknown(tmp_path: Path) -> None:
    """A journal that exists but cannot be parsed is not a success: the
    "Дайджест" button must not tell the owner the pass went fine when the
    only record of that pass is unreadable."""
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{not json", encoding="utf-8")

    status = menu_handler._read_enrich_pass_status(tmp_path)

    assert status.status == "unknown"


def test_read_enrich_pass_status_non_dict_json_is_unknown(tmp_path: Path) -> None:
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("[1, 2, 3]", encoding="utf-8")

    status = menu_handler._read_enrich_pass_status(tmp_path)

    assert status.status == "unknown"


def test_read_enrich_pass_status_reads_real_no_work_journal(tmp_path: Path) -> None:
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"status": "no-work"}), encoding="utf-8")

    status = menu_handler._read_enrich_pass_status(tmp_path)

    assert status.status == "no-work"
    assert status.error == ""


def test_read_enrich_pass_status_reads_real_error_journal(tmp_path: Path) -> None:
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"status": "failed", "error": "qmd unavailable"}), encoding="utf-8"
    )

    status = menu_handler._read_enrich_pass_status(tmp_path)

    assert status.status == "failed"
    assert status.error == "qmd unavailable"


async def test_deliver_daily_digest_suppresses_on_quiet_day_per_real_journal(
    monkeypatch, tmp_path: Path
) -> None:
    """Reproduces defect 5: before the fix, ``pass_status`` was hardcoded to
    ``PassStatus(status="success")``, so ``build_daily_digest`` could never
    see a real "no-work" pass and the quiet-day suppression branch was
    unreachable from this button. Now the real journal drives it."""
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"status": "no-work"}), encoding="utf-8")

    status = FakeStatusMessage()
    _stub_answer_text(monkeypatch, status)
    seen_pass_status = []

    def fake_build_daily_digest(vault_path, day, *, pass_status):  # noqa: ANN001
        seen_pass_status.append(pass_status)
        return None if pass_status.status == "no-work" else "**Дайджест**"

    monkeypatch.setattr(menu_handler, "build_daily_digest", fake_build_daily_digest)

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append((text, kwargs))

    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)

    await menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path)

    assert seen_pass_status[0].status == "no-work"
    assert status.edits == [("Сегодня без изменений — дайджест не нужен.", {})]


# -- defect 4: stale queue-item index could resolve to a different item --


async def test_handle_menu_callback_queueact_mismatched_fingerprint_blocks_action(
    monkeypatch,
) -> None:
    """Reproduces defect 4: the button embeds the index the list had when it
    was rendered. If ``session.queue_items`` has since shifted (e.g. an
    earlier response shortened it), the same index can silently resolve to a
    different item. The fingerprint carried in callback_data must catch this
    and refuse to act instead of applying the action to the wrong page."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900001
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    stale_item = QueueItem(
        kind="conflict",
        page="compiled/decisions/old.md",
        summary="s",
        since="2026-01-01",
    )
    # Index 0 now resolves to a different item than the one the button was
    # built for.
    session.queue_items = [
        QueueItem(
            kind="conflict",
            page="compiled/decisions/new.md",
            summary="s",
            since="2026-02-02",
        )
    ]

    apply_calls: list[tuple] = []
    monkeypatch.setattr(
        menu_handler,
        "apply_response",
        lambda vault_path, item, action_id: apply_calls.append(
            (vault_path, item, action_id)
        ),
    )
    render_calls: list[str | None] = []

    async def fake_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append(notice)

    monkeypatch.setattr(menu_handler, "render_queue", fake_render_queue)

    stale_fingerprint = queue_item_fingerprint(stale_item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{stale_fingerprint}:keep_existing", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert apply_calls == []
    assert render_calls == [menu_handler._QUEUE_ITEM_STALE_MESSAGE]
    assert query.answer_calls == [(menu_handler._QUEUE_ITEM_STALE_MESSAGE, True)]


async def test_handle_menu_callback_queueact_matching_fingerprint_applies_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900002
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/new.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    apply_calls: list[tuple] = []

    def fake_apply_response(vault_path, passed_item, action_id):  # noqa: ANN001
        apply_calls.append((vault_path, passed_item, action_id))
        return ResponseOutcome(ok=True, message="Готово.")

    monkeypatch.setattr(menu_handler, "apply_response", fake_apply_response)
    render_calls: list[str | None] = []

    async def fake_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append(notice)

    monkeypatch.setattr(menu_handler, "render_queue", fake_render_queue)

    fingerprint = queue_item_fingerprint(item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{fingerprint}:keep_existing", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert apply_calls == [(Path("vault"), item, "keep_existing")]
    assert render_calls == ["Готово."]
    assert query.answer_calls == [(None, False)]


async def test_handle_menu_callback_queueact_file_not_found_shows_stale_not_crash(
    monkeypatch,
) -> None:
    """Resilience review defect 2: a duplicate response (double tap, or a
    retried Telegram callback) can lose a race inside
    CompiledBriefingService._archive_candidate. Before that race was fixed
    at the source, the loser saw an uncaught FileNotFoundError -- this
    handler only caught ValueError, so the callback crashed and left the
    owner's button spinning until Telegram's own timeout. Regardless of
    the source-level fix, this is the correct backstop at the bot boundary:
    apply_response's own module docstring already treats a page that
    vanished mid-response as a graceful outcome everywhere else."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900004
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="fact-check-rejected",
        page="compiled/decisions/new.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    def raising_apply_response(vault_path, passed_item, action_id):  # noqa: ANN001
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(menu_handler, "apply_response", raising_apply_response)
    render_calls: list[str | None] = []

    async def fake_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append(notice)

    monkeypatch.setattr(menu_handler, "render_queue", fake_render_queue)

    fingerprint = queue_item_fingerprint(item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{fingerprint}:reject", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert render_calls == []
    assert query.answer_calls == [(menu_handler._QUEUE_ITEM_STALE_MESSAGE, True)]


# -- Task 3: on-demand weekly review screen --


def _empty_weekly_review(review_pick: tuple[str, str] | None = None) -> WeeklyReview:
    return WeeklyReview(
        start=date(2026, 7, 30),
        end=date(2026, 8, 5),
        queue_items=(),
        changes=(),
        revisit=(),
        review_pick=review_pick,
    )


async def test_handle_menu_callback_weekly_renders_weekly_review_screen(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900003
    get_dashboard_session(chat_id)

    render_calls: list[dict] = []

    async def fake_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append(
            {
                "chat_id": chat_id,
                "vault_path": vault_path,
                "preferred_message_id": preferred_message_id,
                "notice": notice,
            }
        )

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )

    query = FakeCallbackQuery("menu:weekly", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert render_calls == [
        {
            "chat_id": chat_id,
            "vault_path": "vault",
            "preferred_message_id": 42,
            "notice": None,
        }
    ]
    assert query.answer_calls == [(None, False)]


async def test_handle_menu_callback_weeklyreview_no_open_screen_blocks_gracefully(
    monkeypatch,
) -> None:
    """The confirm button can only exist once the weekly screen has been
    rendered (it carries the picked page's fingerprint). If it somehow
    arrives without ``session.weekly_review`` set -- e.g. a stale button
    from a previous bot restart -- refuse instead of crashing."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900004
    session = get_dashboard_session(chat_id)
    session.weekly_review = None

    render_calls: list[dict] = []

    async def fake_render_weekly_review(*args, **kwargs):  # noqa: ANN002, ANN003
        render_calls.append(kwargs)

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )
    mark_calls: list[tuple] = []
    monkeypatch.setattr(
        menu_handler,
        "mark_page_human_reviewed",
        lambda *a, **k: mark_calls.append((a, k)),  # noqa: ANN002, ANN003
    )

    query = FakeCallbackQuery("menu:weeklyreview:deadbeef", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert mark_calls == []
    assert render_calls == []
    assert query.answer_calls == [
        (menu_handler._WEEKLY_REVIEW_STALE_MESSAGE, True)
    ]


async def test_handle_menu_callback_weeklyreview_mismatched_fingerprint_blocks_action(
    monkeypatch,
) -> None:
    """Same staleness principle as the queue's index+fingerprint check
    (defect 4): the screen was refreshed and the suggested page changed
    between render and tap, so the confirm must not silently apply to the
    wrong page."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900005
    session = get_dashboard_session(chat_id)
    session.weekly_review = _empty_weekly_review(
        review_pick=("compiled/topics/aurora.md", "Проект Аврора")
    )

    render_calls: list[dict] = []

    async def fake_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append({"notice": notice})

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )
    mark_calls: list[tuple] = []
    monkeypatch.setattr(
        menu_handler,
        "mark_page_human_reviewed",
        lambda *a, **k: mark_calls.append((a, k)),  # noqa: ANN002, ANN003
    )

    query = FakeCallbackQuery("menu:weeklyreview:stalefp0", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert mark_calls == []
    assert render_calls == [{"notice": menu_handler._WEEKLY_REVIEW_STALE_MESSAGE}]
    assert query.answer_calls == [
        (menu_handler._WEEKLY_REVIEW_STALE_MESSAGE, True)
    ]


async def test_handle_menu_callback_weeklyreview_matching_fingerprint_marks_reviewed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900006
    session = get_dashboard_session(chat_id)
    rel_path = "compiled/topics/aurora.md"
    session.weekly_review = _empty_weekly_review(
        review_pick=(rel_path, "Проект Аврора")
    )

    render_calls: list[dict] = []

    async def fake_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append({"notice": notice})

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )
    mark_calls: list[tuple] = []

    def fake_mark_page_human_reviewed(vault_path, passed_rel_path):  # noqa: ANN001
        mark_calls.append((vault_path, passed_rel_path))
        return ResponseOutcome(ok=True, message="Отмечено как просмотренное.")

    monkeypatch.setattr(
        menu_handler, "mark_page_human_reviewed", fake_mark_page_human_reviewed
    )

    fingerprint = page_fingerprint(rel_path)
    query = FakeCallbackQuery(
        f"menu:weeklyreview:{fingerprint}", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert mark_calls == [(Path("vault"), rel_path)]
    assert render_calls == [{"notice": "Отмечено как просмотренное."}]
    assert query.answer_calls == [(None, False)]


async def test_queue_action_write_failure_tells_the_owner_instead_of_hanging(
    monkeypatch,
) -> None:
    """``apply_response`` patches the page first, then removes the queue
    entry, writes the response journal and rewrites the queue document --
    none of which is protected. A failure there (a full disk, a read-only
    vault) used to escape this handler, which only caught ``ValueError`` and
    ``FileNotFoundError``, leaving the button spinning until Telegram's own
    timeout while the page and the queue had already diverged."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900007
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="fact-check-rejected",
        page="compiled/decisions/new.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    def raising_apply_response(vault_path, passed_item, action_id):  # noqa: ANN001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(menu_handler, "apply_response", raising_apply_response)
    render_calls: list[str | None] = []

    async def fake_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append(notice)

    monkeypatch.setattr(menu_handler, "render_queue", fake_render_queue)

    fingerprint = queue_item_fingerprint(item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{fingerprint}:reject", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert render_calls == []
    assert query.answer_calls == [(menu_handler._QUEUE_ACTION_FAILED_MESSAGE, True)]


async def test_weekly_review_mark_failure_tells_the_owner_instead_of_hanging(
    monkeypatch,
) -> None:
    """Same boundary, other button: ``mark_page_human_reviewed`` writes to
    the page and was called with no protection at all."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900008
    session = get_dashboard_session(chat_id)
    rel_path = "compiled/topics/aurora.md"
    session.weekly_review = _empty_weekly_review(
        review_pick=(rel_path, "Проект Аврора")
    )

    render_calls: list[dict] = []

    async def fake_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        render_calls.append({"notice": notice})

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )

    def raising_mark(vault_path, passed_rel_path):  # noqa: ANN001
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(menu_handler, "mark_page_human_reviewed", raising_mark)

    fingerprint = page_fingerprint(rel_path)
    query = FakeCallbackQuery(
        f"menu:weeklyreview:{fingerprint}", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert render_calls == []
    assert query.answer_calls == [(menu_handler._QUEUE_ACTION_FAILED_MESSAGE, True)]


async def test_queue_redraw_failure_still_confirms_the_applied_decision(
    monkeypatch,
) -> None:
    """``apply_response`` already wrote the owner's decision into the vault by
    the time the screen is redrawn. ``render_queue`` re-reads every
    ``compiled/**`` page, which the nightly pass can be moving underneath it,
    and a failure there used to escape the handler before ``query.answer()``
    -- leaving the button spinning as if nothing had happened, which invites
    a second tap on a decision that is already applied."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900009
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="fact-check-rejected",
        page="compiled/decisions/new.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    applied: list[str] = []

    def fake_apply_response(vault_path, passed_item, action_id):  # noqa: ANN001
        applied.append(action_id)
        return SimpleNamespace(ok=True, message="Ответ записан.")

    monkeypatch.setattr(menu_handler, "apply_response", fake_apply_response)

    async def raising_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        raise RuntimeError("page archived mid-render")

    monkeypatch.setattr(menu_handler, "render_queue", raising_render_queue)

    fingerprint = queue_item_fingerprint(item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{fingerprint}:reject", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert applied == ["reject"]
    assert query.answer_calls == [(menu_handler._SCREEN_REDRAW_FAILED_MESSAGE, True)]


async def test_weekly_review_redraw_failure_still_confirms_the_applied_mark(
    monkeypatch,
) -> None:
    """Same boundary, other button: the page is already marked reviewed when
    ``render_weekly_review`` runs."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900010
    session = get_dashboard_session(chat_id)
    rel_path = "compiled/topics/aurora.md"
    session.weekly_review = _empty_weekly_review(
        review_pick=(rel_path, "Проект Аврора")
    )

    marked: list[str] = []

    def fake_mark(vault_path, passed_rel_path):  # noqa: ANN001
        marked.append(passed_rel_path)
        return SimpleNamespace(ok=True, message="Отмечено.")

    monkeypatch.setattr(menu_handler, "mark_page_human_reviewed", fake_mark)

    async def raising_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        raise RuntimeError("compiled tree vanished mid-render")

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", raising_render_weekly_review
    )

    fingerprint = page_fingerprint(rel_path)
    query = FakeCallbackQuery(
        f"menu:weeklyreview:{fingerprint}", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert marked == [rel_path]
    assert query.answer_calls == [(menu_handler._SCREEN_REDRAW_FAILED_MESSAGE, True)]


async def test_handle_menu_callback_queueitem_mismatched_fingerprint_refuses_to_open(
    monkeypatch,
) -> None:
    """The *open* button carried only an index while the action buttons
    carried a fingerprint (code review). An older queue message keeps live
    buttons whenever ``render_dashboard`` falls back to sending a new one --
    it does that for any edit failure other than "message is not modified"
    -- so index 0 of that old page silently resolved against whatever page
    ``session.queue_items`` holds now, and opened someone else's item."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900010
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    stale_item = QueueItem(
        kind="conflict",
        page="compiled/decisions/old.md",
        summary="s",
        since="2026-01-01",
    )
    session.queue_items = [
        QueueItem(
            kind="conflict",
            page="compiled/decisions/new.md",
            summary="s",
            since="2026-02-02",
        )
    ]

    rendered: list[int] = []

    async def fake_render_queue_item(
        bot, *, chat_id, index, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        rendered.append(index)
        return session.queue_items[index]

    monkeypatch.setattr(menu_handler, "render_queue_item", fake_render_queue_item)

    stale_fingerprint = queue_item_fingerprint(stale_item)
    query = FakeCallbackQuery(
        f"menu:queueitem:0:{stale_fingerprint}", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert rendered == []
    assert query.answer_calls == [(menu_handler._QUEUE_ITEM_STALE_MESSAGE, True)]


async def test_handle_menu_callback_queueitem_matching_fingerprint_opens_the_item(
    monkeypatch,
) -> None:
    """Happy path for the check above: the live button still opens."""
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 900011
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/live.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    rendered: list[int] = []

    async def fake_render_queue_item(
        bot, *, chat_id, index, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        rendered.append(index)
        return session.queue_items[index]

    monkeypatch.setattr(menu_handler, "render_queue_item", fake_render_queue_item)

    query = FakeCallbackQuery(
        f"menu:queueitem:0:{queue_item_fingerprint(item)}", chat_id, message_id=42
    )

    await menu_handler.handle_menu_callback(query, bot=object(), state=object())

    assert rendered == [0]
    assert query.answer_calls == [(None, False)]


class FakeBrowserBot:
    """Minimal ``Bot`` double for the file-browser callbacks -- the edit path
    of ``render_dashboard`` plus the document send."""

    def __init__(self, on_send=None) -> None:  # noqa: ANN001
        self.sent: list[str] = []
        self.on_send = on_send

    async def edit_message_text(self, **kwargs) -> None:  # noqa: ANN003
        return None

    async def send_document(self, *, chat_id: int, document) -> None:  # noqa: ANN001
        self.sent.append(str(document.path))
        if self.on_send is not None:
            self.on_send()


def _make_browser_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "daily" / "2026-08").mkdir(parents=True)
    (vault / "daily" / "2026-08" / "note.md").write_text("x", encoding="utf-8")
    return vault


async def test_handle_menu_callback_filesrefresh_survives_a_folder_that_moved(
    monkeypatch, tmp_path: Path
) -> None:
    """``menu:filesroot:`` caught ``FileBrowserError`` and fell back to the
    root picker; refresh/up/page/enter called ``render_file_directory`` bare
    (code review). Every one of them re-reads the folder from disk, and the
    nightly pass archives folders underneath an open browser screen, so the
    raise escaped into aiogram's dispatcher -- which logs and returns. The
    owner got no reply at all: the button just spun until Telegram timed the
    callback out. The folder here is really gone, not monkeypatched."""
    vault = _make_browser_vault(tmp_path)
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path=str(vault))
    )
    chat_id = 900020
    session = get_dashboard_session(chat_id)
    session.file_root_id = "daily"
    session.file_current_dir = "2026-08"
    session.file_page = 0
    shutil.rmtree(vault / "daily" / "2026-08")

    query = FakeCallbackQuery("menu:filesrefresh", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(
        query, bot=FakeBrowserBot(), state=object()
    )

    assert query.answer_calls == [(None, False)]
    assert session.current_screen == "file_roots"


async def test_handle_menu_callback_filesentry_answers_a_failed_telegram_send(
    monkeypatch, tmp_path: Path
) -> None:
    """``send_browser_file`` was guarded by ``except (FileBrowserError,
    OSError)``, but the send fails through aiogram (code review):
    ``TelegramNetworkError`` derives from ``TelegramAPIError``, not
    ``OSError``, so it escaped both handlers and left the button spinning."""
    vault = _make_browser_vault(tmp_path)
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path=str(vault))
    )
    chat_id = 900021

    def boom() -> None:
        raise TelegramNetworkError(method=None, message="connection lost")

    bot = FakeBrowserBot(on_send=boom)
    await render_file_directory(
        bot,
        chat_id=chat_id,
        vault_path=str(vault),
        root_id="daily",
        current_dir="2026-08",
        page=0,
        preferred_message_id=42,
    )
    query = FakeCallbackQuery("menu:filesentry:0", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(query, bot=bot, state=object())

    assert query.answer_calls == [("Не удалось отправить файл.", True)]


async def test_handle_menu_callback_filesentry_sent_file_is_not_reported_as_a_failure(
    monkeypatch, tmp_path: Path
) -> None:
    """The send and the redraw shared one ``try`` (code review). The document
    is in the owner's chat by the time the redraw runs, so a redraw that fails
    -- the nightly pass archiving the folder in between -- came back as
    "❌ Не удалось открыть файл." about a file that had just been delivered,
    inviting a second tap that sends it twice."""
    vault = _make_browser_vault(tmp_path)
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path=str(vault))
    )
    chat_id = 900022

    bot = FakeBrowserBot(on_send=lambda: shutil.rmtree(vault / "daily" / "2026-08"))
    await render_file_directory(
        bot,
        chat_id=chat_id,
        vault_path=str(vault),
        root_id="daily",
        current_dir="2026-08",
        page=0,
        preferred_message_id=42,
    )
    query = FakeCallbackQuery("menu:filesentry:0", chat_id, message_id=42)

    await menu_handler.handle_menu_callback(query, bot=bot, state=object())

    assert len(bot.sent) == 1
    assert query.answer_calls == [(menu_handler._FILE_SENT_REDRAW_FAILED_MESSAGE, True)]
