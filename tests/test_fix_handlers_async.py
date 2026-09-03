"""Tests for the G5 "handlers-async" audit fixes (2026-09-03):

- ``qmd.py`` ran ``subprocess.run`` with no timeout in ``touch_notes``,
  ``cleanup``/``refresh`` (via ``run``) -- a hung ``qmd``/``uv`` process used
  to hang the caller forever (Task A).
- ``why.py``/``brief.py`` called ``QmdService.touch_notes`` synchronously
  inside an async handler, and ``menu.py``/``why.py``/``brief.py``/
  ``dashboard.py`` called their vault-reading builders
  (``build_daily_digest``, ``apply_response``, ``mark_page_human_reviewed``,
  ``build_brief``, ``build_why``, ``list_queue_items``,
  ``collect_weekly_review``, ``FileBrowserService.list_entries``) the same
  way -- each one blocked the whole bot's event loop for as long as it took
  (Tasks B/C).
- ``dashboard.py``'s ``render_dashboard`` could send an oversized document
  fallback with no keyboard once a screen outgrew Telegram's text limit, and
  ``replies.py`` dropped ``reply_markup`` on that same fallback (Task D).

Same FakeMessage/monkeypatch approach the sibling handler test modules use
(``tests/test_why_handler.py``, ``tests/test_brief_handler.py``,
``tests/test_menu_digest.py``).
"""

import asyncio
import subprocess
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from conftest import _write_vault_manifest

from d_brain.bot import dashboard, replies
from d_brain.bot.dashboard import (
    DashboardSession,
    get_dashboard_session,
    page_fingerprint,
)
from d_brain.bot.handlers import brief as brief_handler
from d_brain.bot.handlers import menu as menu_handler
from d_brain.bot.handlers import why as why_handler
from d_brain.services.compiled_briefs import BriefResult
from d_brain.services.compiled_enrich_report import WeeklyReview
from d_brain.services.compiled_why import WhyOutcome, WhyResult
from d_brain.services.decisions_queue import (
    QueueItem,
    ResponseOutcome,
    queue_item_fingerprint,
)
from d_brain.services.file_browser import BrowserRoot
from d_brain.services.qmd import QmdService

# ---------------------------------------------------------------------------
# Shared fakes (same shape as the sibling handler test modules)
# ---------------------------------------------------------------------------


class FakeState:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.cleared = False

    async def get_state(self) -> str | None:
        return None

    async def set_state(self, state: object) -> None:
        pass

    async def clear(self) -> None:
        self.cleared = True

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)


class FakeMessage:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


class FakeCallbackQuery:
    """Same shape as ``tests/test_menu_digest.py``'s double."""

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


def _qmd_service(vault_path: Path, *, qmd_index: str = "dbrain") -> QmdService:
    _write_vault_manifest(vault_path, qmd_index=qmd_index)
    return QmdService(vault_path)


async def _tick(order: list[str]) -> None:
    """A cheap coroutine that must still run promptly while a slow call
    below is in flight -- if that call still ran on the event loop thread
    instead of ``asyncio.to_thread``, this would be starved until it
    finished and ``order`` would read ``["blocking", "tick"]`` instead."""
    await asyncio.sleep(0.05)
    order.append("tick")


def _slow_sync(order: list[str], result: object = None, delay: float = 0.15):
    """Build a sync callable standing in for one of the vault-reading
    builders this fix moves off the event loop."""

    def _call(*args: object, **kwargs: object) -> object:
        time.sleep(delay)
        order.append("blocking")
        return result

    return _call


# ---------------------------------------------------------------------------
# Task A -- qmd.py subprocess timeouts
# ---------------------------------------------------------------------------


def test_qmd_touch_notes_timeout_logs_warning_and_does_not_raise(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    note = vault_path / "daily/example.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Example\n", encoding="utf-8")
    service = _qmd_service(vault_path)

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("d_brain.services.qmd.subprocess.run", fake_run)

    service.touch_notes(["daily/example.md"])  # must not raise


def test_qmd_cleanup_timeout_returns_error_result_without_raising(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)

    def raise_timeout(*args: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=args, timeout=3600, output=b"partial-out", stderr=b"partial-err"
        )

    monkeypatch.setattr(service, "run", raise_timeout)

    result = service.cleanup()

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_qmd_refresh_timeout_on_update_returns_error_without_raising(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)

    def raise_timeout(*args: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="qmd update", timeout=3600)

    monkeypatch.setattr(service, "run", raise_timeout)

    result = service.refresh(with_embeddings=True)

    assert result["updated"] is False
    assert result["embedded"] is False
    assert any("timed out" in error for error in result["errors"])


# ---------------------------------------------------------------------------
# Task B / C -- blocking work moved off the event loop
# ---------------------------------------------------------------------------


async def test_deliver_daily_digest_offloads_build_to_a_thread(
    monkeypatch, tmp_path: Path
) -> None:
    order: list[str] = []
    status = SimpleNamespace(edits=[])

    async def fake_answer_text(message, text: str, **kwargs) -> object:  # noqa: ANN001, ANN003
        return status

    async def fake_edit_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        status.edits.append(text)

    monkeypatch.setattr(menu_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(menu_handler, "edit_text", fake_edit_text)
    # ``None`` is the "no changes today" outcome -- it short-circuits before
    # any vault write, so the test only has to fake the slow build itself.
    monkeypatch.setattr(
        menu_handler, "build_daily_digest", _slow_sync(order, result=None)
    )

    await asyncio.gather(
        menu_handler._deliver_daily_digest(FakeMessage(), vault_path=tmp_path),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_process_why_request_offloads_build_why_to_a_thread(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        why_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    monkeypatch.setattr(
        why_handler,
        "build_why",
        _slow_sync(order, result=WhyOutcome(status="not_found")),
    )

    await asyncio.gather(
        why_handler.process_why_request(FakeMessage(), "запрос", FakeState()),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_handle_why_choice_offloads_build_why_for_path_to_a_thread(
    monkeypatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        why_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    monkeypatch.setattr(
        why_handler, "build_why_for_path", _slow_sync(order, result=None)
    )

    message = FakeMessage()
    state = FakeState()
    rel_path = "compiled/decisions/a.md"
    state.data["why_choices"] = [{"rel_path": rel_path, "title": "Решение А"}]
    token = why_handler.why_choices_token([rel_path])
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:0", chat_id=1, message_id=1)
    query.message = message

    async def fake_get_state() -> str:
        return why_handler.WhyCommandState.waiting_for_input.state

    state.get_state = fake_get_state  # type: ignore[method-assign]

    await asyncio.gather(
        why_handler.handle_why_choice(query, state),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_deliver_why_result_offloads_touch_notes_to_a_thread(monkeypatch) -> None:
    order: list[str] = []

    class SlowQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            time.sleep(0.15)
            order.append("blocking")

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        pass

    monkeypatch.setattr(why_handler, "QmdService", SlowQmd)
    monkeypatch.setattr(why_handler, "answer_rich_text", fake_answer_rich_text)

    result = WhyResult(
        rel_path="compiled/decisions/a.md",
        title="Решение А",
        domain="decisions",
        markdown="**...**",
    )

    await asyncio.gather(
        why_handler.deliver_why_result(FakeMessage(), Path("vault"), result),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


def _sample_brief_result() -> BriefResult:
    return BriefResult(
        brief_type="decision",
        domain="decisions",
        slug="zakupka-servera",
        source_rel_path="compiled/decisions/zakupka-servera.md",
        title="Закупка сервера",
        markdown="**Бриф**",
    )


async def test_process_brief_request_offloads_build_brief_to_a_thread(
    monkeypatch,
) -> None:
    order: list[str] = []
    result = _sample_brief_result()

    monkeypatch.setattr(
        brief_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    monkeypatch.setattr(
        brief_handler, "build_brief", _slow_sync(order, result=result)
    )
    monkeypatch.setattr(
        brief_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(brief_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        brief_handler, "brief_path", lambda vault_path, result, today: "brief.md"
    )
    monkeypatch.setattr(
        brief_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    monkeypatch.setattr(brief_handler, "render_brief_note", lambda result, today: b"x")

    class FakeQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            pass

    monkeypatch.setattr(brief_handler, "QmdService", FakeQmd)

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        pass

    monkeypatch.setattr(brief_handler, "answer_rich_text", fake_answer_rich_text)

    await asyncio.gather(
        brief_handler.process_brief_request(FakeMessage(), "decision", "закупка"),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_handle_menu_callback_queueact_offloads_apply_response_to_a_thread(
    monkeypatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 950001
    session = get_dashboard_session(chat_id)
    session.queue_page = 0
    item = QueueItem(
        kind="conflict",
        page="compiled/decisions/new.md",
        summary="s",
        since="2026-02-02",
    )
    session.queue_items = [item]

    monkeypatch.setattr(
        menu_handler,
        "apply_response",
        _slow_sync(order, result=ResponseOutcome(ok=True, message="Готово.")),
    )

    async def fake_render_queue(
        bot, *, chat_id, vault_path, page=0, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        pass

    monkeypatch.setattr(menu_handler, "render_queue", fake_render_queue)

    fingerprint = queue_item_fingerprint(item)
    query = FakeCallbackQuery(
        f"menu:queueact:0:{fingerprint}:keep_existing", chat_id, message_id=42
    )

    await asyncio.gather(
        menu_handler.handle_menu_callback(query, bot=object(), state=object()),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_handle_menu_callback_weeklyreview_offloads_mark_reviewed_to_a_thread(
    monkeypatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        menu_handler, "get_settings", lambda: SimpleNamespace(vault_path="vault")
    )
    chat_id = 950002
    session = get_dashboard_session(chat_id)
    rel_path = "compiled/topics/aurora.md"
    session.weekly_review = WeeklyReview(
        start=date(2026, 7, 30),
        end=date(2026, 8, 5),
        queue_items=(),
        changes=(),
        revisit=(),
        review_pick=(rel_path, "Проект Аврора"),
    )

    monkeypatch.setattr(
        menu_handler,
        "mark_page_human_reviewed",
        _slow_sync(
            order, result=ResponseOutcome(ok=True, message="Отмечено.")
        ),
    )

    async def fake_render_weekly_review(
        bot, *, chat_id, vault_path, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        pass

    monkeypatch.setattr(
        menu_handler, "render_weekly_review", fake_render_weekly_review
    )

    fingerprint = page_fingerprint(rel_path)
    query = FakeCallbackQuery(
        f"menu:weeklyreview:{fingerprint}", chat_id, message_id=42
    )

    await asyncio.gather(
        menu_handler.handle_menu_callback(query, bot=object(), state=object()),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_render_queue_offloads_list_queue_items_to_a_thread(
    monkeypatch, tmp_path: Path
) -> None:
    order: list[str] = []
    monkeypatch.setattr(dashboard, "list_queue_items", _slow_sync(order, result=[]))

    async def fake_render_dashboard(
        bot, *, chat_id, session, text, keyboard, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        pass

    monkeypatch.setattr(dashboard, "render_dashboard", fake_render_dashboard)

    await asyncio.gather(
        dashboard.render_queue(object(), chat_id=950010, vault_path=tmp_path),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_render_weekly_review_offloads_collect_weekly_review_to_a_thread(
    monkeypatch, tmp_path: Path
) -> None:
    order: list[str] = []
    review = WeeklyReview(
        start=date(2026, 7, 30),
        end=date(2026, 8, 5),
        queue_items=(),
        changes=(),
        revisit=(),
        review_pick=None,
    )
    monkeypatch.setattr(
        dashboard, "collect_weekly_review", _slow_sync(order, result=review)
    )

    async def fake_render_dashboard(
        bot, *, chat_id, session, text, keyboard, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        pass

    monkeypatch.setattr(dashboard, "render_dashboard", fake_render_dashboard)

    await asyncio.gather(
        dashboard.render_weekly_review(object(), chat_id=950011, vault_path=tmp_path),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


async def test_render_file_directory_offloads_list_entries_to_a_thread(
    monkeypatch, tmp_path: Path
) -> None:
    order: list[str] = []
    root = BrowserRoot(id="daily", label="Daily", relative_path="daily")

    class SlowBrowser:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def list_entries(self, *, root_id: str, current_dir: str):
            time.sleep(0.15)
            order.append("blocking")
            return (root, current_dir, [])

    monkeypatch.setattr(dashboard, "FileBrowserService", SlowBrowser)

    async def fake_render_dashboard(
        bot, *, chat_id, session, text, keyboard, preferred_message_id=None, notice=None
    ):  # noqa: ANN001
        pass

    monkeypatch.setattr(dashboard, "render_dashboard", fake_render_dashboard)

    await asyncio.gather(
        dashboard.render_file_directory(
            object(), chat_id=950012, vault_path=tmp_path, root_id="daily"
        ),
        _tick(order),
    )

    assert order == ["tick", "blocking"]


# ---------------------------------------------------------------------------
# Task D -- oversized dashboard screens must keep their keyboard
# ---------------------------------------------------------------------------


def _sample_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Домой", callback_data="menu:home")]
        ]
    )


async def test_render_dashboard_truncates_oversized_text_before_editing() -> None:
    keyboard = _sample_keyboard()
    session = DashboardSession(chat_id=1, dashboard_message_id=42)
    edits: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kwargs) -> None:  # noqa: ANN003
            edits.append(kwargs)

    await dashboard.render_dashboard(
        FakeBot(),
        chat_id=1,
        session=session,
        text="a" * 5000,
        keyboard=keyboard,
    )

    assert len(edits) == 1
    assert len(edits[0]["text"]) <= 4096
    assert edits[0]["reply_markup"] is keyboard
    assert session.dashboard_message_id == 42


async def test_render_dashboard_truncation_accounts_for_markdown_escaping() -> None:
    keyboard = _sample_keyboard()
    session = DashboardSession(chat_id=1, dashboard_message_id=42)
    edits: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kwargs) -> None:  # noqa: ANN003
            edits.append(kwargs)

    # Punctuation-heavy Cyrillic: every '.', '-', '(' and ')' doubles when
    # escaped for MarkdownV2, so a raw 4000-character cut still overflows.
    line = "- 2026-09-03 (изменено): цель [[проект]] — 1.5 ч. (см. ниже).\n"
    await dashboard.render_dashboard(
        FakeBot(),
        chat_id=1,
        session=session,
        text=line * 150,
        keyboard=keyboard,
    )

    assert len(edits) == 1
    assert len(edits[0]["text"]) <= 4096
    assert "экран обрезан" in edits[0]["text"]
    assert edits[0]["reply_markup"] is keyboard


async def test_render_dashboard_truncates_oversized_text_before_sending(
    monkeypatch,
) -> None:
    keyboard = _sample_keyboard()
    session = DashboardSession(chat_id=2)
    sent_calls: list[dict] = []

    async def fake_send_text(*, chat_id, text, bot, **kwargs):  # noqa: ANN001, ANN003
        sent_calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=99)

    monkeypatch.setattr(dashboard, "send_text", fake_send_text)

    await dashboard.render_dashboard(
        object(),
        chat_id=2,
        session=session,
        text="a" * 5000,
        keyboard=keyboard,
    )

    assert len(sent_calls) == 1
    assert len(sent_calls[0]["text"]) <= 4096
    assert sent_calls[0]["reply_markup"] is keyboard
    assert session.dashboard_message_id == 99


class _FakeMessageWithDocument:
    def __init__(self) -> None:
        self.document_calls: list[dict] = []

    async def answer_document(self, document, **kwargs) -> object:  # noqa: ANN003
        self.document_calls.append(kwargs)
        return SimpleNamespace(document=document)


class _FakeBotWithDocument:
    def __init__(self) -> None:
        self.document_calls: list[dict] = []

    async def send_document(self, *, chat_id, document, **kwargs) -> object:  # noqa: ANN003
        self.document_calls.append({"chat_id": chat_id, **kwargs})
        return SimpleNamespace(document=document)


async def test_answer_text_document_fallback_passes_reply_markup() -> None:
    keyboard = _sample_keyboard()
    message = _FakeMessageWithDocument()

    await replies.answer_text(message, "a" * 6000, reply_markup=keyboard)

    assert len(message.document_calls) == 1
    assert message.document_calls[0]["reply_markup"] is keyboard


async def test_send_text_document_fallback_passes_reply_markup() -> None:
    keyboard = _sample_keyboard()
    bot = _FakeBotWithDocument()

    await replies.send_text(bot, chat_id=123, text="a" * 6000, reply_markup=keyboard)

    assert len(bot.document_calls) == 1
    assert bot.document_calls[0]["chat_id"] == 123
    assert bot.document_calls[0]["reply_markup"] is keyboard
