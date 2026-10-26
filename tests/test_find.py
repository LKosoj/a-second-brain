"""/find: keyword search over the vault through ``qmd search`` (G6, no model)."""

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import FSInputFile
from conftest import _write_vault_manifest

from d_brain.bot import dashboard
from d_brain.bot import main as bot_main
from d_brain.bot.handlers import (
    do,
    why,
)
from d_brain.bot.handlers import (
    find as find_handler,
)
from d_brain.bot.states import DoCommandState, FindCommandState, WhyCommandState
from d_brain.services import vault_find
from d_brain.services.qmd import QmdService
from d_brain.services.telegram_markup import (
    markdown_to_markdown_v2,
    normalize_markdown_input,
)
from d_brain.services.vault_find import (
    FindHit,
    FindUnavailable,
    find_in_vault,
    is_findable_rel_path,
    normalize_query,
    resolve_findable_file,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for rel in (
        "daily/2026-01-01.md",
        "notes/a.md",
        "notes/b.md",
        ".session/handoff.md",
        "skills/private/x.md",
        "a/.hidden/x.md",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("text", encoding="utf-8")
    (root / "notes" / "c.txt").write_text("x", encoding="utf-8")
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    (root / "notes" / "link-skills.md").symlink_to(root / "skills/private/x.md")
    (root / "notes" / "link-out.md").symlink_to(tmp_path / "outside.md")
    (root / "notes" / "link-session.md").symlink_to(root / ".session/handoff.md")
    _write_vault_manifest(root)
    return root


def _fake_qmd(monkeypatch, stdout="[]", returncode=0, exc=None):  # noqa: ANN001, ANN202
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(list(command))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(command, returncode, stdout, "boom")

    monkeypatch.setattr("d_brain.services.qmd.run_in_process_group", fake_run)
    return calls


def _hit(rel: str, **extra: object) -> dict[str, object]:
    return {"file": f"qmd://core/{rel}?index=dbrain", "score": 0.5, **extra}


# --- service ---------------------------------------------------------------


def test_command_puts_query_after_double_dash(vault, monkeypatch) -> None:  # noqa: ANN001
    calls = _fake_qmd(monkeypatch)

    assert find_in_vault(vault, "--rm -rf") == []

    command = calls[0]
    assert command[-4:] == ["-n", "15", "--", "--rm -rf"]
    assert "search" in command and "--json" in command
    assert "query" not in command


def test_results_limit_order_dupes_title_snippet(vault, monkeypatch) -> None:  # noqa: ANN001
    for i in range(7):
        (vault / "notes" / f"n{i}.md").write_text("x", encoding="utf-8")
    items = [
        _hit(
            "notes/n0.md", title="Первый", snippet="@@ -1,2 @@ (x)\nстрока  один\n\nдва"
        ),
        _hit("notes/n0.md", title="Дубль"),
        _hit("notes/n1.md", snippet="я" * 500),
        *[_hit(f"notes/n{i}.md") for i in range(2, 7)],
    ]
    _fake_qmd(monkeypatch, json.dumps(items))

    hits = find_in_vault(vault, "q")

    assert [h.rel_path for h in hits] == [f"notes/n{i}.md" for i in range(5)]
    assert hits[0].title == "Первый"
    assert hits[0].snippet == "строка один два"
    assert hits[1].title == "n1"
    assert len(hits[1].snippet) <= vault_find.SNIPPET_MAX_CHARS
    assert hits[1].snippet.endswith("…")


def test_unsafe_hits_are_dropped(vault, monkeypatch) -> None:  # noqa: ANN001
    items = [
        _hit(".session/handoff.md"),
        _hit("skills/private/x.md"),
        _hit("a/.hidden/x.md"),
        _hit("../outside.md"),
        _hit("notes/missing.md"),
        _hit("notes/c.txt"),
        {"file": "qmd://nope/notes/a.md"},
        _hit("notes/link-skills.md"),
        _hit("notes/link-out.md"),
        _hit("notes/link-session.md"),
        _hit("daily/2026-01-01.md"),
    ]
    _fake_qmd(monkeypatch, json.dumps(items))

    assert [h.rel_path for h in find_in_vault(vault, "q")] == ["daily/2026-01-01.md"]


def _write_notes(vault: Path, *rels: str) -> None:
    for rel in rels:
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("text", encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Встреча с Пример Компанией.md", "Встреча-с-Пример-Компанией.md"),
        ("Заметка #1.md", "Заметка-1.md"),
        ("a/Папка (старая)/Файл  v1.2.md", "a/Папка-старая/Файл-v1-2.md"),
        ("x___y.md", "x/y.md"),
        ("Note$1.md", "Note$1.md"),
        ("Note 🎉.md", "Note-1f389.md"),
        ("plain.md", "plain.md"),
        ("---.md", None),
        ("x.md\n", "x-md"),
    ],
)
def test_qmd_handelize(name: str, expected: str | None) -> None:
    assert vault_find.qmd_handelize(name) == expected


def test_normalized_uri_finds_real_file(vault, monkeypatch) -> None:  # noqa: ANN001
    _write_notes(vault, "notes/Встреча с Пример Компанией.md", "notes/Заметка #1.md")
    items = [
        _hit("notes/Встреча-с-Пример-Компанией.md", title="Встреча"),
        _hit("notes/Заметка-1.md"),
        # An exact name still wins and is not duplicated by the fallback.
        _hit("notes/a.md"),
    ]
    _fake_qmd(monkeypatch, json.dumps(items))

    hits = find_in_vault(vault, "q")

    assert [h.rel_path for h in hits] == [
        "notes/Встреча с Пример Компанией.md",
        "notes/Заметка #1.md",
        "notes/a.md",
    ]


def test_normalized_uri_walks_vault_once(vault, monkeypatch) -> None:  # noqa: ANN001
    _write_notes(vault, "notes/Один #1.md", "notes/Два #2.md", "notes/Три #3.md")
    items = [_hit(f"notes/{n}.md") for n in ("Один-1", "Два-2", "Три-3")]
    _fake_qmd(monkeypatch, json.dumps(items))
    walks: list[object] = []
    real_walk = vault_find.os.walk

    def counting_walk(top, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        walks.append(top)
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(vault_find.os, "walk", counting_walk)

    assert len(find_in_vault(vault, "q")) == 3
    assert len(walks) == 1


def test_ambiguous_normalized_uri_is_skipped(vault, monkeypatch) -> None:  # noqa: ANN001
    _write_notes(vault, "notes/Встреча #1.md", "notes/Встреча (1).md")
    _fake_qmd(monkeypatch, json.dumps([_hit("notes/Встреча-1.md")]))

    assert find_in_vault(vault, "q") == []


def test_normalized_uri_never_reaches_forbidden_files(vault, monkeypatch) -> None:  # noqa: ANN001
    _write_notes(
        vault,
        ".session/Встреча с Х.md",
        "a/.hidden/Встреча с Х.md",
        "skills/private/Встреча с Х.md",
    )
    items = [
        _hit(".session/Встреча-с-Х.md"),
        _hit("a/.hidden/Встреча-с-Х.md"),
        _hit("a/hidden/Встреча-с-Х.md"),
        _hit("skills/private/Встреча-с-Х.md"),
    ]
    _fake_qmd(monkeypatch, json.dumps(items))

    assert find_in_vault(vault, "q") == []


@pytest.mark.parametrize(
    "exc", [FileNotFoundError("qmd"), subprocess.TimeoutExpired("qmd", 30)]
)
def test_unavailable_on_exceptions(vault, monkeypatch, exc) -> None:  # noqa: ANN001
    _fake_qmd(monkeypatch, exc=exc)
    with pytest.raises(FindUnavailable):
        find_in_vault(vault, "q")


@pytest.mark.parametrize("stdout", ["not json", '{"a": 1}'])
def test_unavailable_on_bad_output(vault, monkeypatch, stdout) -> None:  # noqa: ANN001
    _fake_qmd(monkeypatch, stdout)
    with pytest.raises(FindUnavailable):
        find_in_vault(vault, "q")


def test_unavailable_on_nonzero_exit(vault, monkeypatch) -> None:  # noqa: ANN001
    _fake_qmd(monkeypatch, "[]", returncode=1)
    with pytest.raises(FindUnavailable):
        find_in_vault(vault, "q")


def test_empty_stdout_is_no_results(vault, monkeypatch) -> None:  # noqa: ANN001
    _fake_qmd(monkeypatch, "  \n")
    assert find_in_vault(vault, "q") == []


def test_resolve_findable_file(vault) -> None:  # noqa: ANN001
    assert (
        resolve_findable_file(vault, "notes/a.md") == (vault / "notes/a.md").resolve()
    )
    assert resolve_findable_file(vault, "notes/link-skills.md") is None
    assert resolve_findable_file(vault, "notes/link-out.md") is None
    assert resolve_findable_file(vault, "notes/") is None


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/etc/x.md",
        "a/../b.md",
        "a//b.md",
        "a\\b.md",
        ".x.md",
        "skills/a.md",
        "a.txt",
    ],
)
def test_is_findable_rel_path_rejects(rel: str) -> None:
    assert is_findable_rel_path(rel) is False


def test_normalize_query() -> None:
    assert normalize_query("  привет\x00\n\tмир ") == "привет мир"
    assert normalize_query("   ") == ""
    assert len(normalize_query("a" * 200)) == 200
    # Length is the handler's concern: the service only cleans the string.
    assert len(normalize_query("a" * 201)) == 201


# --- handler ----------------------------------------------------------------


class FakeState:
    def __init__(self, current_state: str | None = None) -> None:
        self.current_state = current_state
        self.data: dict[str, object] = {}
        self.cleared = False

    async def get_state(self) -> str | None:
        return self.current_state

    async def set_state(self, state: object) -> None:
        self.current_state = getattr(state, "state", str(state))

    async def clear(self) -> None:
        self.cleared = True
        self.current_state = None
        self.data = {}

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.voice = None
        self.forward_origin = None
        self.chat = SimpleNamespace(id=7)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, message: object | None) -> None:
        self.data = data
        self.message = message
        self.answer_calls: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answer_calls.append((text, show_alert))


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []
        self.actions: list[str] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append(action)

    async def send_document(self, **kwargs) -> None:  # noqa: ANN003
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(kwargs)


def _stub(monkeypatch, vault: Path, hits=None, exc=None):  # noqa: ANN001, ANN202
    calls: list[tuple[str, str]] = []

    def fake_find(vault_path, query):  # noqa: ANN001, ANN202
        calls.append((str(vault_path), threading.current_thread().name))
        calls.append(query)  # type: ignore[arg-type]
        if exc is not None:
            raise exc
        return hits or []

    monkeypatch.setattr(
        find_handler, "get_settings", lambda: SimpleNamespace(vault_path=str(vault))
    )
    monkeypatch.setattr(find_handler, "find_in_vault", fake_find)
    return calls


HITS = [
    FindHit("notes/a.md", "Заметка А", "первый фрагмент"),
    FindHit("daily/2026-01-01.md", "День", "второй фрагмент"),
]


async def test_inline_query_searches_and_offers_buttons(vault, monkeypatch) -> None:  # noqa: ANN001
    calls = _stub(monkeypatch, vault, HITS)
    message, state = FakeMessage(), FakeState(WhyCommandState.waiting_for_input.state)

    await find_handler.cmd_find(message, SimpleNamespace(args="запрос"), state)

    assert state.cleared is True
    assert calls[1] == "запрос"
    assert calls[0][1] != threading.current_thread().name
    text, kwargs = message.answers[0]
    assert "notes/a.md" in text or "notes/a\\.md" in text
    assert "первый" in text
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert len(buttons) == 2
    for i, button in enumerate(buttons):
        assert button.callback_data.startswith("find:open:")
        assert button.callback_data.endswith(f":{i}")
        assert len(button.callback_data.encode()) <= 64


async def test_no_args_starts_wait_then_cancels(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    message, state = FakeMessage(), FakeState()

    await find_handler.cmd_find(message, SimpleNamespace(args=None), state)
    assert state.current_state == FindCommandState.waiting_for_input.state
    assert "find_entered_at" in state.data

    await find_handler.cmd_find(message, SimpleNamespace(args=None), state)
    assert state.current_state is None
    assert "отменён" in message.answers[-1][0]


async def test_expired_wait_restarts(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    state = FakeState(FindCommandState.waiting_for_input.state)
    state.data = {"find_entered_at": time.time() - 601}

    await find_handler.cmd_find(FakeMessage(), SimpleNamespace(args=None), state)

    assert state.current_state == FindCommandState.waiting_for_input.state
    assert state.data["find_entered_at"] > time.time() - 5


async def test_wait_input(vault, monkeypatch) -> None:  # noqa: ANN001
    calls = _stub(monkeypatch, vault, HITS)
    state = FakeState(FindCommandState.waiting_for_input.state)
    message = FakeMessage("слово")
    await find_handler.handle_find_input(message, state)
    assert calls[1] == "слово" and state.cleared

    state = FakeState(FindCommandState.waiting_for_input.state)
    message = FakeMessage("-")
    await find_handler.handle_find_input(message, state)
    assert "отменён" in message.answers[0][0] and state.cleared


@pytest.mark.parametrize("kind", ["voice", "forward", "expired", "cmd", "path"])
async def test_wait_input_skips(vault, monkeypatch, kind) -> None:  # noqa: ANN001
    calls = _stub(monkeypatch, vault, HITS)
    state = FakeState(FindCommandState.waiting_for_input.state)
    message = FakeMessage("текст")
    if kind == "voice":
        message.text = None
    elif kind == "forward":
        message.forward_origin = object()
    elif kind == "expired":
        state.data = {"find_entered_at": time.time() - 601}
    elif kind == "cmd":
        message.text = "/do"
    else:
        message.text = "/etc/hosts"

    with pytest.raises(SkipHandler):
        await find_handler.handle_find_input(message, state)

    assert state.cleared is True
    assert calls == []


async def test_empty_and_long_query(vault, monkeypatch) -> None:  # noqa: ANN001
    calls = _stub(monkeypatch, vault, HITS)
    message = FakeMessage()
    await find_handler.process_find_request(message, "   ", FakeState())
    await find_handler.process_find_request(message, "я" * 201, FakeState())
    assert calls == []
    assert "200" in message.answers[1][0]


async def test_no_results(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault, [])
    message = FakeMessage()
    await find_handler.process_find_request(message, "ничего", FakeState())
    text, kwargs = message.answers[0]
    assert "ничего не нашёл" in text
    assert "reply_markup" not in kwargs


@pytest.mark.parametrize("exc", [FindUnavailable("x"), ValueError("boom")])
async def test_failures_answer_once(vault, monkeypatch, exc) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault, exc=exc)
    message = FakeMessage()
    await find_handler.process_find_request(message, "q", FakeState())
    assert len(message.answers) == 1
    assert "недоступен" in message.answers[0][0]


HOSTILE = "<b>x</b> ** [a](tg://user?id=1) ` ``` \\ _ ~ | # ![i](attachments/a.png)"


async def test_hostile_text_is_rendered_safely(vault, monkeypatch) -> None:  # noqa: ANN001
    hits = [FindHit("notes/a.md", HOSTILE, HOSTILE)]
    _stub(monkeypatch, vault, hits)
    message = FakeMessage()

    await find_handler.process_find_request(message, HOSTILE[:100], FakeState())

    raw = message.answers[0][0]
    assert "<" not in raw and ">" not in raw
    rendered = markdown_to_markdown_v2(normalize_markdown_input(raw))
    # Links may survive only as literal text inside code spans/blocks.
    outside_code = re.sub(r"```.*?```|`[^`]*`", "", rendered, flags=re.DOTALL)
    assert "](" not in outside_code
    assert "<b>" not in rendered
    # one message, not an HTML document fallback
    assert message.answers[0][1]["parse_mode"] == "MarkdownV2"


def _opened(vault: Path, hits=HITS):  # noqa: ANN001, ANN202
    state = FakeState()
    paths = [h.rel_path for h in hits]
    state.data = {
        "find_results": paths,
        "find_token": find_handler._results_token(paths),
    }
    return state


async def test_open_sends_file(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    state, bot = _opened(vault), FakeBot()
    token = state.data["find_token"]
    query = FakeCallback(f"find:open:{token}:0", FakeMessage())

    await find_handler.handle_find_open(query, state, bot)

    assert len(bot.sent) == 1
    document = bot.sent[0]["document"]
    assert isinstance(document, FSInputFile) and document.filename == "a.md"
    assert bot.sent[0]["chat_id"] == 7
    assert query.answer_calls == [(None, False)]


@pytest.mark.parametrize(
    "data", ["find:open:deadbeef:0", "find:open:{t}:9", "find:open:{t}:x"]
)
async def test_open_stale(vault, monkeypatch, data) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    state, bot = _opened(vault), FakeBot()
    query = FakeCallback(data.format(t=state.data["find_token"]), FakeMessage())

    await find_handler.handle_find_open(query, state, bot)

    assert bot.sent == []
    assert query.answer_calls[0][1] is True
    assert "устарел" in (query.answer_calls[0][0] or "")


async def test_open_without_saved_data(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    bot = FakeBot()
    query = FakeCallback("find:open:abcd:0", FakeMessage())
    await find_handler.handle_find_open(query, FakeState(), bot)
    assert bot.sent == [] and "устарел" in (query.answer_calls[0][0] or "")


async def test_open_without_message(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    bot = FakeBot()
    query = FakeCallback("find:open:abcd:0", None)
    await find_handler.handle_find_open(query, _opened(vault), bot)
    assert bot.sent == [] and query.answer_calls == [(None, False)]


async def test_open_file_replaced_by_symlink(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    state, bot = _opened(vault), FakeBot()
    target = vault / "notes" / "a.md"
    target.unlink()
    target.symlink_to(vault / "skills/private/x.md")
    query = FakeCallback(f"find:open:{state.data['find_token']}:0", FakeMessage())

    await find_handler.handle_find_open(query, state, bot)

    assert bot.sent == []
    assert query.answer_calls == [("Файл больше не доступен.", True)]


async def test_open_too_large(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    monkeypatch.setattr(find_handler, "TELEGRAM_MAX_DOCUMENT_BYTES", 1)
    state, bot = _opened(vault), FakeBot()
    query = FakeCallback(f"find:open:{state.data['find_token']}:0", FakeMessage())

    await find_handler.handle_find_open(query, state, bot)

    assert bot.sent == []
    assert "слишком большой" in (query.answer_calls[0][0] or "")


async def test_open_send_failure(vault, monkeypatch) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    state, bot = _opened(vault), FakeBot(fail=True)
    query = FakeCallback(f"find:open:{state.data['find_token']}:0", FakeMessage())

    await find_handler.handle_find_open(query, state, bot)

    assert query.answer_calls == [("Не удалось отправить файл.", True)]


# --- wiring -----------------------------------------------------------------


def test_router_order_and_help() -> None:
    dp = bot_main.create_dispatcher()
    names = [r.name for r in dp.sub_routers]
    assert names.index("edit") < names.index("find") < names.index("menu")
    for later in ("do", "why", "brief", "text"):
        assert names.index("find") < names.index(later)
    assert "/find" in dashboard.build_help_text()


async def test_configure_bot_commands_lists_find() -> None:
    seen: list[list[str]] = []

    class Bot:
        async def set_my_commands(self, commands, **kwargs) -> None:  # noqa: ANN001, ANN003
            seen.append([c.command for c in commands])

    await bot_main.configure_bot_commands(Bot())  # type: ignore[arg-type]
    assert "find" in seen[0]


def test_qmd_public_uri_mapping(vault) -> None:  # noqa: ANN001
    svc = QmdService(vault)
    assert svc.uri_to_rel_path("qmd://daily/x.md") == "daily/x.md"
    assert svc.uri_to_rel_path("qmd://nope/x.md") is None
    # Real qmd appends the index name when run with ``--index``.
    assert svc.uri_to_rel_path("qmd://daily/x.md?index=dbrain") == "daily/x.md"
    assert svc.uri_to_rel_path("qmd://daily/x.md#frag") == "daily/x.md"
    assert svc.uri_to_rel_path("qmd://daily/x.md?index=a#frag") == "daily/x.md"
    assert DoCommandState  # imported for router-state parity


@pytest.mark.parametrize(
    "state", [WhyCommandState.waiting_for_input, DoCommandState.waiting_for_input]
)
async def test_find_command_beats_other_wait_handlers(monkeypatch, state) -> None:  # noqa: ANN001
    from datetime import datetime
    from unittest.mock import AsyncMock

    from aiogram import Bot, Dispatcher, Router
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, MessageEntity, Update, User

    searched: list[str] = []
    received: list[Message] = []

    async def fake_process(message, query, st) -> None:  # noqa: ANN001
        searched.append(query)

    monkeypatch.setattr(find_handler, "process_find_request", fake_process)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(find_handler.router)  # same position as in main: first
    dp.include_router(why.router)
    dp.include_router(do.router)
    sink = Router(name="sink")

    @sink.message()
    async def catch_all(message: Message) -> None:
        received.append(message)

    dp.include_router(sink)
    bot = Bot("123456:TEST")
    bot.session.make_request = AsyncMock()  # type: ignore[method-assign]
    chat, user = Chat(id=5, type="private"), User(id=5, is_bot=False, first_name="o")
    await dp.storage.set_state(StorageKey(bot_id=bot.id, chat_id=5, user_id=5), state)
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=chat,
        from_user=user,
        text="/find abc",
        entities=[MessageEntity(type="bot_command", offset=0, length=5)],
    )

    await dp.feed_update(bot, Update(update_id=1, message=message))

    assert searched == ["abc"]
    assert received == []


@pytest.mark.parametrize("rel", ["Skills/private/x.md", "SKILLS/x.md"])
def test_skills_check_ignores_case(rel: str) -> None:
    assert not is_findable_rel_path(rel)


@pytest.mark.parametrize("data", ["find:open", "find:open:abcd", "find:open:abcd:"])
async def test_open_truncated_callback_data(vault, monkeypatch, data) -> None:  # noqa: ANN001
    _stub(monkeypatch, vault)
    bot = FakeBot()
    query = FakeCallback(data, FakeMessage())

    await find_handler.handle_find_open(query, FakeState(), bot)

    assert bot.sent == []
    assert query.answer_calls[0][1] is True
    assert "устарел" in (query.answer_calls[0][0] or "")


async def test_open_stale_answer_tolerates_expired_callback(vault, monkeypatch) -> None:  # noqa: ANN001
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery

    _stub(monkeypatch, vault)

    class Expired(FakeCallback):
        async def answer(self, text=None, show_alert=False) -> None:  # noqa: ANN001
            raise TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="1"),
                message="query is too old",
            )

    query = Expired("find:open:abcd:0", FakeMessage())
    await find_handler.handle_find_open(query, FakeState(), FakeBot())
