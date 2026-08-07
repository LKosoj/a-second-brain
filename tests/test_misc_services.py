import io
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.logging import LogCaptureFixture
from conftest import _load_memory_engine, _write_vault_manifest

from d_brain import run_daily_process, run_plaud_sync, run_telegram_delivery
from d_brain.bot.formatters import truncate_plain_text_for_edit
from d_brain.bot.handlers.photo import _build_photo_daily_content
from d_brain.manifest import ManifestValidationError, load_manifest_for_vault
from d_brain.services import telegram_delivery
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.image_analysis import ImageAnalysisService
from d_brain.services.memory_audit import MemoryAuditService
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.processor import (
    SCHEDULED_MODE,
)
from d_brain.services.reflection_digest import ReflectionDigestService
from d_brain.services.session import SessionStore
from d_brain.services.source_links import (
    SourceInfo,
    build_telegram_source_info,
)
from d_brain.services.todoist_projects import (
    TodoistProjectCatalog,
    TodoistProjectRouter,
)


def _inject_memory_engine_write_before_lock(
    monkeypatch,
    memory_engine,
    vault_path: Path,
    note_path: Path,
    concurrent_content: str,
) -> None:
    """Install one cooperative write immediately before a command gets the lock."""
    original_lock = memory_engine.vault_write_lock
    manifest = load_manifest_for_vault(vault_path)
    injected = False

    @contextmanager
    def lock_with_concurrent_write(root: Path):
        nonlocal injected
        with original_lock(root) as lock:
            if not injected:
                write_validated_vault_markdown(
                    vault_path,
                    note_path,
                    concurrent_content.encode("utf-8"),
                    manifest=manifest,
                    existing_lock=lock,
                )
                injected = True
            yield lock

    monkeypatch.setattr(memory_engine, "vault_write_lock", lock_with_concurrent_write)


def test_session_store_reads_recent_entries_from_daily_files(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    user_id = 42
    today = date.today()
    yesterday = today - timedelta(days=1)
    yesterday_entry = {
        "ts": f"{yesterday}T10:00:00+03:00",
        "type": "text",
        "text": "y",
    }
    today_voice_entry = {
        "ts": f"{today}T09:00:00+03:00",
        "type": "voice",
        "text": "a",
    }
    today_text_entry = {
        "ts": f"{today}T10:00:00+03:00",
        "type": "text",
        "text": "b",
    }

    yesterday_path = store._get_session_file(user_id, yesterday)
    yesterday_path.write_text(
        json.dumps(yesterday_entry) + "\n",
        encoding="utf-8",
    )

    today_path = store._get_session_file(user_id, today)
    today_path.write_text(
        "\n".join(
            [
                json.dumps(today_voice_entry),
                json.dumps(today_text_entry),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    recent = store.get_recent(user_id, limit=2)
    today_entries = store.get_today(user_id)
    stats = store.get_stats(user_id, days=2)

    assert [entry["text"] for entry in recent] == ["a", "b"]
    assert [entry["text"] for entry in today_entries] == ["a", "b"]
    assert stats == {"text": 2, "voice": 1}


def test_session_store_logs_corrupted_lines_but_keeps_valid_entries(
    tmp_path: Path,
    caplog,
) -> None:
    store = SessionStore(tmp_path)
    user_id = 43
    today = date.today()
    session_path = store._get_session_file(user_id, today)
    session_path.write_text(
        "\n".join(
            [
                '{"ts":"2026-04-04T10:00:00+03:00","type":"text","text":"ok"}',
                '{"ts":"broken"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        entries = store.get_today(user_id)

    assert [entry["text"] for entry in entries] == ["ok"]
    assert "Skipping corrupted session entry" in caplog.text


def test_memory_audit_service_flags_duplicate_prone_clusters(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "MEMORY.md").write_text(
        """---
type: note
---
# Долгосрочная память

## Ключевые решения

**2026-04-04** — `/process` должен оставаться preview-only.
**2026-04-04** — `/process_full` и кнопка `Обработать` запускают полный цикл.
**2026-04-04** — Prompt-layer должен быть context-driven без lexical hardcode.
**2026-04-04** — Owner-assignment должен жить в prompt-layer, а не в Python.

## Изученное

### Правила для запоминания
- Не добавляй lexical hardcode в Python для owner-assignment.
- `/process` не должен выполнять write-heavy полный цикл.
""",
        encoding="utf-8",
    )

    report = MemoryAuditService(vault_path).audit()
    rendered = report.to_markdown()

    assert report.has_issues is True
    assert {finding.theme.key for finding in report.findings} == {
        "process_modes",
        "prompt_layer",
    }
    assert "Processing mode contract duplicated" in rendered
    assert "Prompt-layer / no-hardcode policy duplicated" in rendered
    assert "decision L8" in rendered
    assert "rule L16" in rendered


def test_memory_audit_reads_top_level_memory_rules(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "MEMORY.md").write_text(
        """# Долгосрочная память

## Ключевые решения

**2026-04-04** — `/process` должен оставаться preview-only.

## Правила для запоминания
- `/process` не должен выполнять write-heavy полный цикл.
""",
        encoding="utf-8",
    )

    items = MemoryAuditService(vault_path)._extract_items()

    assert [item.section for item in items] == ["decision", "rule"]


def test_daily_entry_memory_store_reads_json_config_with_frontmatter(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / ".memory-config.json").write_text(
        """---
last_accessed: 2026-03-11
---
{"tiers": {"active": 3, "warm": 9, "cold": 30}, "decay_rate": 0.05}
""",
        encoding="utf-8",
    )

    store = DailyEntryMemoryStore(vault_path)

    assert store._config["tiers"]["active"] == 3
    assert store._config["tiers"]["warm"] == 9
    assert store._config["tiers"]["cold"] == 30
    assert store._config["decay_rate"] == 0.05


def test_scheduled_digest_uses_daily_processed_entries() -> None:
    digest = run_daily_process._build_scheduled_digest(
        date(2026, 4, 4),
        {
            "processed_entries": 5,
            "daily": {"processed_entries": 2},
            "periodic_cycles": [{"name": "maintenance.compiled-nightly", "result": {}}],
        },
        {},
    )

    assert "Записей обработано: **2**" in digest


def test_memory_engine_uses_floor_adjusted_exponential_decay() -> None:
    memory_engine = _load_memory_engine()

    assert memory_engine.calc_relevance(0, 0.015, 0.1) == 1.0
    assert memory_engine.calc_relevance(10000, 0.015, 0.1) == 0.1
    assert memory_engine.calc_relevance(30, 0.015, 0.1) < 1.0
    assert memory_engine.calc_relevance(30, 0.015, 0.1) > 0.1


def test_memory_engine_daily_only_updates_daily_notes_under_shared_lock(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    daily_path = vault_path / "daily" / "2026-04-04.md"
    dated_thought = vault_path / "thoughts" / "2026-04-04.md"
    daily_path.parent.mkdir(parents=True)
    dated_thought.parent.mkdir(parents=True)
    daily_path.write_text("# 2026-04-04\n", encoding="utf-8")
    dated_thought.write_text("# Dated thought\n", encoding="utf-8")
    memory_engine = _load_memory_engine()

    memory_engine.cmd_daily(vault_path, memory_engine.DEFAULT_CONFIG)

    daily = daily_path.read_text(encoding="utf-8")
    assert 'type: "daily"' in daily
    assert 'date: "2026-04-04"' in daily
    assert dated_thought.read_text(encoding="utf-8") == "# Dated thought\n"
    assert (vault_path / ".locks" / "vault-write.lock").exists()


def test_memory_engine_daily_requires_valid_manifest(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    memory_engine = _load_memory_engine()

    with pytest.raises(ManifestValidationError, match="is missing"):
        memory_engine.cmd_daily(vault_path, memory_engine.DEFAULT_CONFIG)


def test_memory_engine_init_generates_complete_thought_card_profile(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "ideas" / "new-direction.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# New Direction\n\nBody.\n", encoding="utf-8")
    memory_engine = _load_memory_engine()

    memory_engine.cmd_init(vault_path, memory_engine.DEFAULT_CONFIG)

    content = note_path.read_text(encoding="utf-8")
    assert 'type: "note"' in content
    assert 'description: "New Direction"' in content
    assert "tags: [memory, note]" in content
    assert 'status: "active"' in content
    assert "created:" in content
    assert "updated:" in content
    assert content.endswith("# New Direction\n\nBody.\n")


def test_memory_engine_init_skips_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "ideas" / "new-direction.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# New Direction\n\nOriginal body.\n", encoding="utf-8")
    concurrent_content = (
        "---\n"
        'type: "note"\n'
        'description: "Concurrent description"\n'
        "tags: [concurrent, memory]\n"
        'status: "active"\n'
        'created: "2026-07-29"\n'
        'updated: "2026-07-29"\n'
        'last_accessed: "2026-07-29"\n'
        "relevance: 1.0\n"
        'tier: "core"\n'
        "owner: concurrent-writer\n"
        "---\n"
        "# New Direction\n\nConcurrent body.\n"
    )
    memory_engine = _load_memory_engine()
    _inject_memory_engine_write_before_lock(
        monkeypatch,
        memory_engine,
        vault_path,
        note_path,
        concurrent_content,
    )

    memory_engine.cmd_init(vault_path, memory_engine.DEFAULT_CONFIG)

    assert note_path.read_text(encoding="utf-8") == concurrent_content
    assert "concurrent conflicts skipped: 1" in capsys.readouterr().out


def test_memory_engine_decay_skips_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "notes" / "decay.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2020-01-01\n"
            "relevance: 0.2\n"
            "tier: cold\n"
            "---\n"
            "# Decay\n\nOriginal body.\n"
        ),
        encoding="utf-8",
    )
    concurrent_content = (
        "---\n"
        "type: note\n"
        "last_accessed: 2026-07-29\n"
        "relevance: 1.0\n"
        "tier: core\n"
        "owner: concurrent-writer\n"
        "---\n"
        "# Decay\n\nConcurrent body.\n"
    )
    memory_engine = _load_memory_engine()
    _inject_memory_engine_write_before_lock(
        monkeypatch,
        memory_engine,
        vault_path,
        note_path,
        concurrent_content,
    )

    memory_engine.cmd_decay(vault_path, memory_engine.DEFAULT_CONFIG)

    assert note_path.read_text(encoding="utf-8") == concurrent_content
    assert "concurrent conflicts skipped: 1" in capsys.readouterr().out


def test_memory_engine_excludes_private_skills_from_vault_cards(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    note_path = vault_path / "thoughts" / "note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# Note\n", encoding="utf-8")
    private_skill = vault_path / "skills/private/local-skill/SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("# Private skill\n", encoding="utf-8")

    memory_engine = _load_memory_engine()

    assert memory_engine.find_cards(vault_path, memory_engine.DEFAULT_CONFIG) == [
        note_path
    ]


def test_memory_engine_daily_skips_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "daily" / "2026-04-04.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# 2026-04-04\n\nOriginal body.\n", encoding="utf-8")
    concurrent_content = (
        "---\n"
        "type: daily\n"
        "date: 2026-04-04\n"
        "last_accessed: 2026-07-29\n"
        "relevance: 1.0\n"
        "tier: core\n"
        "owner: concurrent-writer\n"
        "---\n"
        "# 2026-04-04\n\nConcurrent body.\n"
    )
    memory_engine = _load_memory_engine()
    _inject_memory_engine_write_before_lock(
        monkeypatch,
        memory_engine,
        vault_path,
        note_path,
        concurrent_content,
    )

    memory_engine.cmd_daily(vault_path, memory_engine.DEFAULT_CONFIG)

    assert note_path.read_text(encoding="utf-8") == concurrent_content
    assert "concurrent conflicts skipped: 1" in capsys.readouterr().out


def test_memory_engine_touch_recomputes_from_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "notes" / "touch.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-01-01\n"
            "relevance: 0.4\n"
            "tier: cold\n"
            "---\n"
            "# Touch\n\nOriginal body.\n"
        ),
        encoding="utf-8",
    )
    today = date.today().isoformat()
    concurrent_content = (
        "---\n"
        "type: note\n"
        f'last_accessed: "{today}"\n'
        "relevance: 1.0\n"
        'tier: "core"\n'
        "owner: concurrent-writer\n"
        "---\n"
        "# Touch\n\nConcurrent body.\n"
    )
    memory_engine = _load_memory_engine()
    _inject_memory_engine_write_before_lock(
        monkeypatch,
        memory_engine,
        vault_path,
        note_path,
        concurrent_content,
    )

    memory_engine.cmd_touch(str(note_path), memory_engine.DEFAULT_CONFIG)

    content = note_path.read_text(encoding="utf-8")
    assert 'tier: "core"' in content
    assert "owner: concurrent-writer" in content
    assert content.endswith("# Touch\n\nConcurrent body.\n")


def test_todoist_project_catalog_refresh_parses_projects(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    (vault_path.parent / "mcp-config.json").write_text("{}", encoding="utf-8")
    catalog = TodoistProjectCatalog(vault_path, todoist_api_key="todoist-token")

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"structuredContent":{"projects":['
                '{"id":"inbox-id","name":"Inbox","inboxProject":true,"childOrder":0},'
                '{"id":"work-id","name":"Рабочие","inboxProject":false,"childOrder":1}'
                "]}}"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    refreshed = catalog.refresh()

    assert refreshed["inbox_project_id"] == "inbox-id"
    assert refreshed["projects"][1]["name"] == "Рабочие"
    assert (vault_path / ".sync" / "todoist-projects.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([{"id": "project-id"}], id="list"),
        pytest.param("invalid root", id="scalar"),
    ],
)
def test_todoist_project_catalog_rejects_non_object_cache(
    tmp_path: Path,
    caplog: LogCaptureFixture,
    payload: object,
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    catalog = TodoistProjectCatalog(vault_path)
    catalog._cache_path.parent.mkdir(parents=True)
    catalog._cache_path.write_text(json.dumps(payload), encoding="utf-8")

    assert catalog.load_cached() is None
    assert "Invalid Todoist project cache" in caplog.text


def test_todoist_project_router_accepts_exact_project_hint(tmp_path: Path) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    catalog = TodoistProjectCatalog(vault_path, todoist_api_key="todoist-token")
    router = TodoistProjectRouter(
        vault_path,
        todoist_api_key="todoist-token",
        catalog=catalog,
    )

    decision = router.route_task(
        {"content": "Купить подарок", "project_hint": "Покупки"},
        catalog={
            "fetched_at": "2026-04-04T10:00:00+00:00",
            "inbox_project_id": "inbox-id",
            "projects": [
                {"id": "shopping-id", "name": "Покупки"},
                {"id": "personal-id", "name": "Личные"},
            ],
        },
    )

    assert decision["project_id"] == "shopping-id"
    assert decision["confidence"] == "high"


def test_reflection_digest_service_returns_takeaways_from_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = ReflectionDigestService(
        tmp_path / "vault",
        ai_cli="qwen",
        content_language="ru",
    )

    monkeypatch.setattr(
        service.runner,
        "run",
        lambda prompt, timeout: (  # noqa: ARG005
            '```json\n{"takeaways":["Первый вывод","Второй вывод"]}\n```'
        ),
    )

    takeaways = service.summarize(
        day=date(2026, 4, 5),
        report_markdown="**Report**",
        execute_payload={
            "thoughts_saved": [{"title": "Архитектура qmd", "category": "learnings"}],
            "tasks_created": [],
            "crm_updated": [],
        },
    )

    assert takeaways == ["Первый вывод", "Второй вывод"]


def test_image_analysis_service_parses_fenced_json(tmp_path: Path) -> None:
    image_path = tmp_path / "vault" / "attachments" / "2026-04-04" / "img-test.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png")

    service = ImageAnalysisService(tmp_path / "vault", "qwen")

    def fake_run(prompt, timeout, extra_env=None):  # noqa: ANN001, ANN202, ARG001
        assert timeout == 600
        return '```json\n{"description":"Слайд с надписью","ocr_text":"HELLO 123"}\n```'

    service.runner.run = fake_run  # type: ignore[method-assign]

    result = service.analyze("attachments/2026-04-04/img-test.png")

    assert result == {
        "description": "Слайд с надписью",
        "ocr_text": "HELLO 123",
    }


def test_build_telegram_source_info_public_and_private() -> None:
    public_message = SimpleNamespace(
        message_id=42,
        link=None,
        chat=SimpleNamespace(id=-1009876543210, type="supergroup", username="testchat"),
    )
    private_message = SimpleNamespace(
        message_id=43,
        link=None,
        chat=SimpleNamespace(id=123456789, type="private", username="userhandle"),
    )

    public_source = build_telegram_source_info(public_message)
    private_source = build_telegram_source_info(private_message)

    assert public_source.ref == "telegram:-1009876543210:42"
    assert public_source.url == "https://t.me/testchat/42"
    assert public_source.label == "Открыть сообщение в Telegram"
    assert private_source.ref == "telegram:123456789:43"
    assert private_source.url == ""


def test_build_photo_daily_content_includes_caption_description_and_ocr() -> None:
    content = _build_photo_daily_content(
        "attachments/2026-04-04/img-100000.jpg",
        "Подпись пользователя",
        {
            "description": "На фото белая доска с планом.",
            "ocr_text": "ROADMAP\nQ2",
        },
        SourceInfo(
            kind="telegram",
            ref="telegram:1:100",
            url="https://t.me/testchat/100",
            label="Open Telegram message",
        ),
    )

    assert "![[attachments/2026-04-04/img-100000.jpg]]" in content
    assert "Источник: [Open Telegram message](https://t.me/testchat/100)" in content
    assert "Идентификатор источника: `telegram:1:100`" in content
    assert "Подпись пользователя" in content
    assert "AI-описание:" in content
    assert "На фото белая доска с планом." in content
    assert "OCR:" in content
    assert "ROADMAP\nQ2" in content


def test_build_album_daily_content_keeps_one_caption_and_all_images() -> None:
    from d_brain.bot.handlers.photo import PhotoEntry, _build_album_daily_content

    first = PhotoEntry(
        message_id=10,
        timestamp=datetime(2026, 4, 4, 12, 0, 0),
        relative_path="attachments/2026-04-04/img-120000-10.jpg",
        caption="Общий комментарий к альбому",
        analysis={"description": "Первая картинка", "ocr_text": "ONE"},
        source=SourceInfo(
            kind="telegram",
            ref="telegram:1:10",
            url="https://t.me/testchat/10",
            label="Open Telegram message",
        ),
        content_language="ru",
    )
    second = PhotoEntry(
        message_id=11,
        timestamp=datetime(2026, 4, 4, 12, 0, 1),
        relative_path="attachments/2026-04-04/img-120001-11.jpg",
        caption=None,
        analysis={"description": "Вторая картинка", "ocr_text": "TWO"},
        source=SourceInfo(kind="telegram", ref="telegram:1:11"),
        content_language="ru",
    )

    content = _build_album_daily_content([first, second])

    assert content.count("Общий комментарий к альбому") == 1
    assert "### Фото 1" in content
    assert "### Фото 2" in content
    assert "![[attachments/2026-04-04/img-120000-10.jpg]]" in content
    assert "![[attachments/2026-04-04/img-120001-11.jpg]]" in content
    assert "Идентификатор источника: `telegram:1:10`" in content
    assert "Идентификатор источника: `telegram:1:11`" in content
    assert "Первая картинка" in content
    assert "Вторая картинка" in content


async def test_flush_album_marks_the_whole_group_forwarded_if_any_photo_is(
    monkeypatch,
) -> None:
    """Fail closed: one forwarded photo taints the grouped entry, so a
    single owned photo's ``[photo]`` marker cannot vouch for the rest and
    let a forwarded source inherit "own" trust. Previously unpinned -- the
    whole album flush could fall back to ``items[0].entry_type`` and the
    suite stayed green."""
    from d_brain.bot.handlers import photo as photo_handler

    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0)

    def _entry(message_id: int, *, entry_type: str, forwarded: bool):
        return photo_handler.PhotoEntry(
            message_id=message_id,
            timestamp=datetime(2026, 4, 4, 12, 0, 0),
            relative_path=f"attachments/2026-04-04/img-{message_id}.jpg",
            caption=None,
            analysis=None,
            source=SourceInfo(kind="telegram", ref=f"telegram:1:{message_id}"),
            content_language="ru",
            entry_type=entry_type,
            forwarded=forwarded,
        )

    # The owned photo sorts first, so a naive ``items[0]`` picks "[photo]".
    photo_handler._album_items["group-1"] = [
        _entry(10, entry_type="[photo]", forwarded=False),
        _entry(11, entry_type="[forward from: Bob]", forwarded=True),
    ]
    appended: list[tuple[str, str]] = []

    class FakeStorage:
        def append_to_daily(self, content: str, timestamp, entry_type: str) -> None:  # noqa: ANN001
            appended.append((content, entry_type))

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        return None

    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)

    await photo_handler._flush_album("group-1", object(), FakeStorage())

    assert [entry_type for _content, entry_type in appended] == [
        "[forward from: Bob]"
    ]


async def test_flush_album_failed_confirmation_is_not_reported_as_a_lost_album(
    monkeypatch,
) -> None:
    """The grouped daily entry is written before the confirmation is sent,
    and the two used to share one ``try``: a failed send landed in the
    handler that answers "❌ Ошибка сохранения альбома", telling the owner an
    album that is on disk had been lost. Same defect the single-photo path
    and the other capture handlers were fixed for."""
    from d_brain.bot.handlers import photo as photo_handler

    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0)

    photo_handler._album_items["group-2"] = [
        photo_handler.PhotoEntry(
            message_id=10,
            timestamp=datetime(2026, 4, 4, 12, 0, 0),
            relative_path="attachments/2026-04-04/img-10.jpg",
            caption=None,
            analysis=None,
            source=SourceInfo(kind="telegram", ref="telegram:1:10"),
            content_language="ru",
            entry_type="[photo]",
            forwarded=False,
        )
    ]
    appended: list[str] = []
    sent: list[str] = []

    class FakeStorage:
        def append_to_daily(self, content: str, timestamp, entry_type: str) -> None:  # noqa: ANN001
            appended.append(entry_type)

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        sent.append(text)
        raise RuntimeError("send failed")

    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)

    await photo_handler._flush_album("group-2", object(), FakeStorage())

    assert appended == ["[photo]"]
    assert not [text for text in sent if text.startswith("❌")]


def test_send_telegram_text_sync_uses_configured_owner_target(
    monkeypatch,
) -> None:
    sent: list[tuple[str, int, str, str | None, bool]] = []

    async def fake_send_to_target(
        token: str,
        chat_id: int,
        text: str,
        *,
        parse_mode=None,
        rich: bool = False,
    ) -> None:
        sent.append((token, chat_id, text, parse_mode, rich))

    monkeypatch.setattr(
        telegram_delivery,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(
        telegram_delivery,
        "_send_telegram_text_to_target",
        fake_send_to_target,
    )

    telegram_delivery.send_telegram_text_sync(
        "test",
        parse_mode="HTML",
        rich=True,
    )

    assert sent == [("bot-token", 42, "test", "HTML", True)]


def test_telegram_delivery_main_sends_stdin_as_rich_report(monkeypatch) -> None:
    sent: list[tuple[str, bool]] = []
    monkeypatch.setattr(sys, "stdin", io.StringIO("# Итог\n\n- Готово\n"))
    monkeypatch.setattr(
        run_telegram_delivery,
        "send_telegram_text_sync",
        lambda text, *, rich=False: sent.append((text, rich)),
    )

    assert run_telegram_delivery.main() == 0
    assert sent == [("# Итог\n\n- Готово", True)]


def test_truncate_plain_text_for_edit_marks_dynamic_reply_length() -> None:
    text = "x" * 5000

    truncated = truncate_plain_text_for_edit(text)

    assert len(truncated) <= 4000
    assert truncated.endswith("✂️")


def test_run_daily_process_sends_scheduled_digest_with_changes(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, str | None, bool]] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            assert day == date(2026, 4, 5)
            assert mode == SCHEDULED_MODE
            session_dir = tmp_path / ".session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "execute.json").write_text(
                json.dumps(
                    {
                        "tasks_created": [{"content": "Проверить ranking qmd"}],
                        "thoughts_saved": [
                            {
                                "title": "Архитектура qmd",
                                "path": "thoughts/learnings/x.md",
                            }
                        ],
                        "crm_updated": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "report": "📊 **Обработка завершена**",
                "processed_entries": 5,
                "mode": SCHEDULED_MODE,
            }

    monkeypatch.setattr(
        run_daily_process,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="qwen",
            owner_full_name="Иван",
            content_language="ru",
            openai_api_key="",
            openai_base_url="",
            openai_model="",
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(
        run_daily_process,
        "CliProcessor",
        lambda *args: FakeProcessor(),
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append(
            (text, parse_mode, rich)
        ),
    )
    monkeypatch.setattr(
        run_daily_process,
        "_build_digest_takeaways",
        lambda settings, day, result: [  # noqa: ARG005
            "День сместился в qmd ranking tuning и recall contract.",
            "MEMORY.md очищен до durable operating context.",
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()
    stdout = capsys.readouterr().out

    assert exit_code == 0
    assert "Обработка завершена" in stdout
    assert sent == [
        (
            (
                "**🧠 D-Brain — 2026-04-05**\n\n"
                "Ежедневная обработка завершена.\n"
                "Записей обработано: **5** | задач: **1** | "
                "мыслей: **1** | CRM: **0**\n\n"
                "**Ключевые выводы**\n"
                "- День сместился в qmd ranking tuning и recall contract.\n"
                "- MEMORY.md очищен до durable operating context.\n\n"
                "**Новые задачи**\n"
                "- Проверить ranking qmd"
            ),
            None,
            True,
        )
    ]


def test_run_daily_process_sends_scheduled_digest_when_nothing_new(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sent: list[tuple[str, bool]] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            assert day == date(2026, 4, 5)
            assert mode == SCHEDULED_MODE
            session_dir = tmp_path / ".session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "execute.json").write_text(
                json.dumps(
                    {
                        "tasks_created": [],
                        "thoughts_saved": [],
                        "crm_updated": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "report": "📊 **Обработка завершена**",
                "processed_entries": 3,
                "mode": SCHEDULED_MODE,
            }

    monkeypatch.setattr(
        run_daily_process,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="qwen",
            owner_full_name="Иван",
            content_language="ru",
            openai_api_key="",
            openai_base_url="",
            openai_model="",
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(
        run_daily_process,
        "CliProcessor",
        lambda *args: FakeProcessor(),
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append((text, rich)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()

    assert exit_code == 0
    assert "По результатам сегодняшнего дня ничего нового." in sent[0][0]
    assert sent[0][1] is True


def test_build_scheduled_digest_keeps_periodic_cycles_when_daily_empty() -> None:
    digest = run_daily_process._build_scheduled_digest(
        date(2026, 4, 5),
        {
            "empty_daily": True,
            "periodic_cycles": [
                {
                    "name": "maintenance.compiled-nightly",
                    "label": "Compiled Maintenance",
                    "result": {"note_path": "vault/.graph/report.md"},
                }
            ],
            "audit_task_candidates": ["todo:maintenance.compiled-nightly"],
        },
        {},
        takeaways=["Nightly maintenance нашёл drift в vault."],
    )

    assert "Сегодня новых записей не было." in digest
    assert "**Ключевые выводы**" in digest
    assert "**Периодические циклы**" in digest
    assert "- Compiled Maintenance: `vault/.graph/report.md`" in digest
    assert "**Найдены проблемы**" in digest
    assert "По результатам сегодняшнего дня ничего нового." not in digest


def test_build_digest_takeaways_allows_periodic_only_empty_daily(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    (tmp_path / ".session").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".session" / "execute.json").write_text(
        json.dumps(
            {
                "tasks_created": [],
                "thoughts_saved": [],
                "crm_updated": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeReflectionDigestService:
        def __init__(self, vault_path: Path, *, ai_cli: str, content_language: str):
            captured["vault_path"] = vault_path
            captured["ai_cli"] = ai_cli
            captured["content_language"] = content_language

        def summarize(
            self,
            *,
            day: date,
            report_markdown: str,
            execute_payload: dict[str, object],
        ) -> list[str]:
            captured["day"] = day
            captured["report_markdown"] = report_markdown
            captured["execute_payload"] = execute_payload
            return ["Nightly maintenance вынес follow-up задачи."]

    monkeypatch.setattr(
        run_daily_process,
        "ReflectionDigestService",
        FakeReflectionDigestService,
    )

    takeaways = run_daily_process._build_digest_takeaways(
        SimpleNamespace(
            vault_path=tmp_path,
            ai_cli="qwen",
            content_language="ru",
        ),
        date(2026, 4, 5),
        {
            "daily": {"empty_daily": True},
            "report": "🧩 **Compiled Maintenance**\nНочью исправлены связи.",
            "periodic_cycles": [{"name": "maintenance.compiled-nightly"}],
        },
    )

    assert takeaways == ["Nightly maintenance вынес follow-up задачи."]
    assert captured["report_markdown"] == (
        "🧩 **Compiled Maintenance**\nНочью исправлены связи."
    )


def test_run_daily_process_sends_scheduled_error_digest(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, bool]] = []

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str):  # noqa: ANN202
            assert day == date(2026, 4, 5)
            assert mode == SCHEDULED_MODE
            return {"error": "capture failed"}

    monkeypatch.setattr(
        run_daily_process,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="",
            ai_cli="qwen",
            owner_full_name="Иван",
            content_language="ru",
            openai_api_key="",
            openai_base_url="",
            openai_model="",
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(
        run_daily_process,
        "CliProcessor",
        lambda *args: FakeProcessor(),
    )
    monkeypatch.setattr(
        run_daily_process,
        "send_telegram_text_sync",
        lambda text, *, parse_mode=None, rich=False: sent.append((text, rich)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_daily_process.py", "--mode", "scheduled", "--date", "2026-04-05"],
    )

    exit_code = run_daily_process.main()
    stderr = capsys.readouterr().err

    assert exit_code == 1
    assert "capture failed" in stderr
    assert "Ежедневная обработка завершилась с ошибкой." in sent[0][0]
    assert sent[0][1] is False


def test_run_plaud_sync_refreshes_qmd_with_embeddings_after_import(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakePlaudService:
        def __init__(self, vault_path: Path, **kwargs) -> None:  # noqa: ANN003
            assert vault_path == tmp_path
            del kwargs

        def sync(self, **kwargs):  # noqa: ANN003, ANN202
            assert kwargs == {
                "backfill": False,
                "allow_retro_todo": True,
                "max_pages": None,
            }
            return {
                "seen": 1,
                "imported": 1,
                "pending_summary": 0,
                "unchanged": 0,
                "tasks_created": 0,
                "dirty_weeks": [],
                "errors": [],
                "qmd": {
                    "available": True,
                    "updated": True,
                    "embedded": True,
                    "errors": [],
                },
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        run_plaud_sync,
        "get_settings",
        lambda: SimpleNamespace(
            plaud_bearer_token="token",
            vault_path=tmp_path,
            plaud_region="api-euc1",
            ai_cli="codex",
            todoist_api_key="",
            owner_full_name="Иван Иванов",
            content_language="ru",
            telegram_bot_token="bot-token",
            owner_telegram_id=42,
        ),
    )
    monkeypatch.setattr(run_plaud_sync, "PlaudSyncService", FakePlaudService)
    monkeypatch.setattr(
        run_plaud_sync,
        "_notify_auth_recovered",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(sys, "argv", ["run_plaud_sync.py"])

    exit_code = run_plaud_sync.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["qmd"] == {
        "available": True,
        "updated": True,
        "embedded": True,
        "errors": [],
    }
