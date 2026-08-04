import asyncio
import fcntl
import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import _write_vault_manifest

from d_brain import run_plaud_sync
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.frontmatter import read_frontmatter, validate_document
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.plaud import (
    PlaudAuthError,
    PlaudClient,
    PlaudSyncAlreadyRunningError,
    PlaudSyncService,
)
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import (
    build_plaud_source_info,
)


class _FakePlaudClient:
    def __init__(
        self,
        items: list[dict[str, object]],
        details: dict[str, dict[str, object]],
    ) -> None:
        self.items = items
        self.details = details

    def iter_recordings(self, *, limit: int = 100, max_pages: int | None = None):  # type: ignore[no-untyped-def]
        del limit, max_pages
        yield from self.items

    def get_recording(self, file_id: str) -> dict[str, object]:
        return self.details[file_id]


@pytest.fixture(autouse=True)
def _plaud_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


def _assert_valid_import_note(vault_path: Path, note_path: Path, body: str) -> None:
    document = read_frontmatter(note_path)
    route, missing, invalid = validate_document(
        note_path.relative_to(vault_path).as_posix(),
        document,
        load_manifest_for_vault(vault_path),
    )

    assert route.name == "import"
    assert missing == ()
    assert invalid == ()
    assert body in document.body.decode("utf-8")


def _reject_direct_markdown_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def protected(path: Path) -> bool:
        return path.suffix == ".md" or "imports" in path.parts or ".sync" in path.parts

    def reject_markdown_write(path: Path, data: str, *args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        if protected(path):
            raise AssertionError(f"direct vault write: {path}")
        return original_write_text(path, data, *args, **kwargs)

    def reject_vault_bytes(path: Path, data: bytes, *args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        if protected(path):
            raise AssertionError(f"direct vault write: {path}")
        return original_write_bytes(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_markdown_write)
    monkeypatch.setattr(Path, "write_bytes", reject_vault_bytes)


def test_plaud_sync_refuses_duplicate_run(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(vault_path, bearer_token="token")
    state_lock = vault_path / ".sync" / "plaud-state.lock"
    state_lock.parent.mkdir(parents=True, exist_ok=True)

    with state_lock.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            try:
                service.sync()
            except PlaudSyncAlreadyRunningError as exc:
                assert str(exc) == "PLAUD sync is already running"
            else:
                raise AssertionError("expected PlaudSyncAlreadyRunningError")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def test_plaud_sync_rejects_vault_symlink_swap_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from d_brain.services.frontmatter import UnsafeVaultPathError

    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    external = tmp_path / "external"
    external.mkdir()
    service = PlaudSyncService(
        vault,
        bearer_token="token",
        client=_FakePlaudClient([], {}),  # type: ignore[arg-type]
    )
    original_manifest = service._manifest_for_writes

    def swap_after_precheck():  # type: ignore[no-untyped-def]
        manifest = original_manifest()
        vault.rename(pinned)
        vault.symlink_to(external, target_is_directory=True)
        return manifest

    monkeypatch.setattr(service, "_manifest_for_writes", swap_after_precheck)

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        service.sync(backfill=True, refresh_qmd=False)

    assert list(external.rglob("*")) == []


def test_plaud_client_iter_recordings_uses_skip_pagination() -> None:
    pages = {
        0: [{"id": "a1", "filename": "one", "start_time": 1712217600000}],
        1: [{"id": "b2", "filename": "two", "start_time": 1712131200000}],
        2: [],
    }
    seen_skips: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_skips.append(int(request.url.params.get("skip", "0")))
        skip = int(request.url.params.get("skip", "0"))
        limit = int(request.url.params.get("limit", "1"))
        assert limit == 1
        assert request.url.params.get("is_trash") == "2"
        assert request.url.params.get("sort_by") == "start_time"
        assert request.url.params.get("is_desc") == "true"
        return httpx.Response(
            200,
            json={"data_file_list": pages.get(skip, [])},
        )

    client = PlaudClient("token")
    client._client = httpx.Client(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler),
        base_url="https://api.plaud.ai",
    )

    items = list(client.iter_recordings(limit=1))

    assert [item["file_id"] for item in items] == ["a1", "b2"]
    assert seen_skips == [0, 1, 2]


def test_plaud_client_raises_auth_error_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"message": "unauthorized"})

    client = PlaudClient("token")
    client._client = httpx.Client(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler),
        base_url="https://api.plaud.ai",
    )

    try:
        client.list_recordings()
    except PlaudAuthError as exc:
        assert "PLAUD auth failed" in str(exc)
    else:
        raise AssertionError("Expected PlaudAuthError")


def test_plaud_auth_alert_deduplicates_and_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "vault" / ".sync" / "plaud-state.json"
    sent_messages: list[str] = []

    async def fake_send(token: str, chat_id: int, text: str) -> None:
        assert token == "bot-token"
        assert chat_id == 42
        sent_messages.append(text)

    def fake_send_sync(text: str, *, parse_mode=None) -> None:
        assert parse_mode is None
        asyncio.run(fake_send("bot-token", 42, text))

    monkeypatch.setattr(run_plaud_sync, "send_telegram_text_sync", fake_send_sync)

    first = run_plaud_sync._notify_auth_failure(
        state_path=state_path,
        error_message="PLAUD auth failed (401) for /file/simple/web",
    )
    second = run_plaud_sync._notify_auth_failure(
        state_path=state_path,
        error_message="PLAUD auth failed (401) for /file/simple/web",
    )
    recovered = run_plaud_sync._notify_auth_recovered(
        state_path=state_path,
    )
    recovered_again = run_plaud_sync._notify_auth_recovered(
        state_path=state_path,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert first is True
    assert second is False
    assert recovered is True
    assert recovered_again is False
    assert len(sent_messages) == 2
    assert "авторизация не удалась" in sent_messages[0]
    assert "авторизация восстановлена" in sent_messages[1]
    assert state["alerts"]["plaud_auth"]["active"] is False
    assert state["alerts"]["plaud_auth"]["last_notified_at"]
    assert state["alerts"]["plaud_auth"]["resolved_at"]


def test_plaud_sync_imports_recordings_and_creates_daily_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    _reject_direct_markdown_write(monkeypatch)
    items = [{"file_id": "file-1"}]
    details = {
        "file-1": {
            "file_id": "file-1",
            "title": "Weekly sync",
            "record_time": 1712217600000,
            "trans_result": "Я отправлю follow-up завтра.",
            "ai_content": {"summary": "Иван должен отправить follow-up."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": False,
            "owner_confidence": "medium",
            "reason": "archive only",
            "tasks": [],
            "search_value": {
                "topics": ["sync"],
                "entities": [],
                "meeting_prep_value": False,
            },
        },
        ensure_ascii=False,
    )

    result = service.sync(backfill=True, refresh_qmd=False)

    assert result["imported"] == 1
    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    raw_files = list((vault_path / "imports" / "plaud" / "raw").rglob("*.json"))
    assert len(note_files) == 1
    assert len(raw_files) == 1
    note_content = note_files[0].read_text(encoding="utf-8")
    daily_content = (vault_path / "daily" / "2024-04-04.md").read_text(encoding="utf-8")
    assert "[plaud]" in daily_content
    assert "<!-- d-brain:entry-status: already_processed -->" in daily_content
    assert "PLAUD: [[imports/plaud/notes/" in daily_content
    plaud_source = build_plaud_source_info("file-1")
    assert plaud_source.url in note_content
    assert plaud_source.ref in note_content
    assert "Иван должен отправить follow-up." in note_content
    assert '"summary"' not in note_content
    _assert_valid_import_note(vault_path, note_files[0], "# Weekly sync")
    assert plaud_source.url in daily_content
    assert plaud_source.ref in daily_content
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    signal = DailyEntryMemoryStore(vault_path).best_signal_for_path(
        "daily/2024-04-04.md"
    )
    assert state["recordings"]["file-1"]["status"] == "imported"
    assert state["recordings"]["file-1"]["source_url"] == plaud_source.url
    assert state["recordings"]["file-1"]["source_ref"] == plaud_source.ref
    assert signal is not None
    assert signal.entries == 1


def test_plaud_daily_stub_repeat_does_not_duplicate_heading(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakePlaudClient([], {}),  # type: ignore[arg-type]
    )
    recorded_at = datetime(2026, 4, 14, 9, 30, tzinfo=UTC)

    service._upsert_daily_stub(
        file_id="file-repeat",
        recorded_at=recorded_at,
        note_rel_path="imports/plaud/notes/repeat.md",
        title="Repeat note",
        summary="First summary.",
    )
    service._upsert_daily_stub(
        file_id="file-repeat",
        recorded_at=recorded_at,
        note_rel_path="imports/plaud/notes/repeat.md",
        title="Repeat note",
        summary="Updated summary.",
    )

    content = (vault_path / "daily" / f"{recorded_at.date().isoformat()}.md").read_text(
        encoding="utf-8"
    )
    assert content.count("## 09:30 [plaud]") == 1
    assert content.count("<!-- plaud:file-repeat:start -->") == 1
    assert content.count("<!-- plaud:file-repeat:end -->") == 1
    assert "First summary." not in content
    assert "Updated summary." in content


def test_plaud_recorded_at_uses_local_timezone_for_numeric_timestamp(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient([], {}),  # type: ignore[arg-type]
    )

    recorded_at = service._recorded_at({"record_time": 1712217600000})

    assert recorded_at == datetime.fromtimestamp(1712217600, tz=UTC).astimezone()


def test_plaud_migrate_note_path_drift_updates_note_state_and_daily(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )

    timestamp_ms = 1714463228000
    detail = {
        "file_id": "file-legacy",
        "title": "Legacy PLAUD note",
        "record_time": timestamp_ms,
        "trans_result": "Полный транскрипт.",
        "ai_content": {"summary": "Полное саммари."},
    }
    raw_path = (
        vault_path / "imports" / "plaud" / "raw" / "2025" / "04" / "file-legacy.json"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient([], {}),  # type: ignore[arg-type]
    )

    recorded_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    recorded_local = recorded_utc.astimezone(timezone(timedelta(hours=3)))
    monkeypatch.setattr(service, "_recorded_at", lambda payload: recorded_local)
    legacy_note_path, _ = service._file_paths("file-legacy", recorded_utc)
    canonical_note_path, _ = service._file_paths("file-legacy", recorded_local)
    legacy_note_rel = legacy_note_path.relative_to(vault_path).as_posix()
    legacy_note_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_note_path.write_text(
        "---\n"
        "type: plaud-recording\n"
        "source: plaud\n"
        "source_id: file-legacy\n"
        "raw_path: imports/plaud/raw/2025/04/file-legacy.json\n"
        "last_accessed: 2026-04-30\n"
        "relevance: 0.5\n"
        "tier: warm\n"
        "---\n\n"
        "# Legacy PLAUD note\n",
        encoding="utf-8",
    )

    daily_path = vault_path / "daily" / f"{recorded_local.date().isoformat()}.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        (
            "---\n"
            "type: daily\n"
            f"date: {recorded_local.date().isoformat()}\n"
            "last_accessed: 2026-04-30\n"
            "relevance: 0.5\n"
            "tier: warm\n"
            "---\n\n"
            f"# {recorded_local.date().isoformat()}\n\n"
            f"## {recorded_utc:%H:%M} [plaud]\n"
            "<!-- plaud:file-legacy:start -->\n"
            f"PLAUD: [[{legacy_note_rel}|Legacy PLAUD note]]\n"
            "> Источник: [Открыть в PLAUD](https://app.plaud.ai/file/file-legacy)\n"
            "> Идентификатор источника: `plaud:file-legacy`\n"
            "Саммари: Старый блок\n"
            "<!-- plaud:file-legacy:end -->\n"
        ),
        encoding="utf-8",
    )
    reference_path = vault_path / "thoughts" / "plaud-drift-check.md"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_text(
        f"Суффикс должен остаться: {legacy_note_rel}-shadow\n",
        encoding="utf-8",
    )

    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "recordings": {
                    "file-legacy": {
                        "status": "imported",
                        "note_path": legacy_note_rel,
                        "raw_path": "imports/plaud/raw/2025/04/file-legacy.json",
                    }
                },
                "dirty_weeks": [],
                "last_sync_at": None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        QmdService,
        "refresh_after_searchable_write",
        lambda self: {  # noqa: ANN001
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        },
    )

    _reject_direct_markdown_write(monkeypatch)
    result = service.migrate_note_path_drift(refresh_qmd=False)

    assert result["migrated"] == 1
    assert not legacy_note_path.exists()
    assert canonical_note_path.exists()
    _assert_valid_import_note(vault_path, canonical_note_path, "# Legacy PLAUD note")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert (
        state["recordings"]["file-legacy"]["note_path"]
        == canonical_note_path.relative_to(vault_path).as_posix()
    )
    daily_content = daily_path.read_text(encoding="utf-8")
    assert daily_content.count(f"## {recorded_utc:%H:%M} [plaud]") == 1
    if f"{recorded_local:%H:%M}" != f"{recorded_utc:%H:%M}":
        assert f"## {recorded_local:%H:%M} [plaud]" not in daily_content
    assert canonical_note_path.relative_to(vault_path).as_posix() in daily_content
    assert legacy_note_rel not in daily_content
    assert (
        reference_path.read_text(encoding="utf-8")
        == f"Суффикс должен остаться: {legacy_note_rel}-shadow\n"
    )


def test_plaud_sync_refreshes_qmd_by_default(tmp_path: Path, monkeypatch) -> None:
    calls: list[bool] = []
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    items = [{"file_id": "file-qmd"}]
    details = {
        "file-qmd": {
            "file_id": "file-qmd",
            "title": "Weekly sync",
            "record_time": 1712217600000,
            "trans_result": "Нужно сохранить запись.",
            "ai_content": {"summary": "Архивируем PLAUD note."},
        }
    }

    def fake_refresh(self):  # noqa: ANN001
        calls.append(True)
        return {
            "available": True,
            "updated": True,
            "embedded": True,
            "errors": [],
        }

    monkeypatch.setattr(QmdService, "refresh_after_searchable_write", fake_refresh)
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": False,
            "owner_confidence": "medium",
            "reason": "archive only",
            "tasks": [],
            "search_value": {
                "topics": [],
                "entities": [],
                "meeting_prep_value": False,
            },
        },
        ensure_ascii=False,
    )

    result = service.sync(backfill=True)

    assert calls == [True]
    assert result["qmd"]["embedded"] is True


def test_plaud_sync_imports_recording_even_if_classification_fails(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-fail"}]
    details = {
        "file-fail": {
            "file_id": "file-fail",
            "title": "Broken classifier",
            "record_time": timestamp_ms,
            "trans_result": "Нужно не потерять эту запись.",
            "ai_content": {
                "summary": "Даже при сбое классификации запись надо сохранить."
            },
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )

    def raise_classification_error(prompt: str) -> str:
        del prompt
        raise RuntimeError("classifier crashed")

    service._run_prompt = raise_classification_error  # type: ignore[method-assign]

    result = service.sync(backfill=True, refresh_qmd=False)

    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    raw_files = list((vault_path / "imports" / "plaud" / "raw").rglob("*.json"))
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert result["imported"] == 1
    assert result["tasks_created"] == 0
    assert len(note_files) == 1
    assert len(raw_files) == 1
    assert "Broken classifier" in note_files[0].read_text(encoding="utf-8")
    assert state["recordings"]["file-fail"]["status"] == "imported"
    assert state["recordings"]["file-fail"]["owner_eval_pending"] is True
    assert (
        state["recordings"]["file-fail"]["classification_error"] == "classifier crashed"
    )


def test_plaud_classification_failure_normalizes_legacy_null_task_ids(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-legacy-null-tasks"}]
    details = {
        "file-legacy-null-tasks": {
            "file_id": "file-legacy-null-tasks",
            "title": "Legacy task state",
            "record_time": timestamp_ms,
            "trans_result": "Нужно сохранить запись.",
            "ai_content": {"summary": "Запись должна импортироваться."},
        }
    }
    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "recordings": {
                    "file-legacy-null-tasks": {
                        "status": "imported",
                        "owner_eval_pending": True,
                        "task_ids": None,
                        "todoist_fingerprint": "",
                    }
                },
                "dirty_weeks": [],
                "last_sync_at": None,
            }
        ),
        encoding="utf-8",
    )
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("classifier crashed")
    )

    result = service.sync(backfill=True, refresh_qmd=False)
    recording_state = json.loads(state_path.read_text(encoding="utf-8"))["recordings"][
        "file-legacy-null-tasks"
    ]

    assert result["imported"] == 1
    assert result["errors"] == []
    assert recording_state["task_ids"] == []
    assert recording_state["classification_error"] == "classifier crashed"


def test_plaud_sync_creates_tasks_only_for_high_confidence_recent_items(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-2"}]
    details = {
        "file-2": {
            "file_id": "file-2",
            "title": "Voice memo",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить письмо завтра.",
            "ai_content": {"summary": "Личная заметка с действием."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    created: list[list[dict[str, object]]] = []
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить письмо",
                    "due_hint": "tomorrow",
                    "priority": 3,
                    "evidence": "Надо отправить письмо завтра.",
                }
            ],
            "search_value": {"topics": [], "entities": [], "meeting_prep_value": False},
        },
        ensure_ascii=False,
    )
    service._create_todoist_tasks = (  # type: ignore[method-assign]
        lambda tasks, **kwargs: created.append(tasks) or ["1"]
    )

    first = service.sync(backfill=True, refresh_qmd=False)
    second = service.sync(backfill=True, refresh_qmd=False)

    assert first["tasks_created"] == 1
    assert second["tasks_created"] == 0
    assert created == [
        [
            {
                "content": "Отправить письмо",
                "due_hint": "tomorrow",
                "priority": 3,
                "evidence": "Надо отправить письмо завтра.",
            }
        ]
    ]


def test_plaud_sync_does_not_retry_when_todoist_is_disabled(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-no-todoist"}]
    details = {
        "file-no-todoist": {
            "file_id": "file-no-todoist",
            "title": "Voice memo",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить письмо завтра.",
            "ai_content": {"summary": "Личная заметка с действием."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="",
        owner_full_name="Иванов Иван",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить письмо",
                    "due_hint": "tomorrow",
                    "priority": 3,
                    "evidence": "Надо отправить письмо завтра.",
                }
            ],
            "search_value": {"topics": [], "entities": [], "meeting_prep_value": False},
        },
        ensure_ascii=False,
    )

    result = service.sync(backfill=True, refresh_qmd=False)
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert result["tasks_created"] == 0
    assert state["recordings"]["file-no-todoist"]["owner_eval_pending"] is False


def test_plaud_sync_retries_recent_owner_eval_after_archive_only_backfill(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-3"}]
    details = {
        "file-3": {
            "file_id": "file-3",
            "title": "Recent meeting",
            "record_time": timestamp_ms,
            "trans_result": "Иванов готовит follow-up.",
            "ai_content": {"summary": "Иванов должен отправить follow-up."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    created: list[list[dict[str, object]]] = []
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "meeting",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить follow-up",
                    "due_hint": "",
                    "priority": 2,
                    "evidence": "Иванов должен отправить follow-up.",
                }
            ],
            "search_value": {"topics": [], "entities": [], "meeting_prep_value": True},
        },
        ensure_ascii=False,
    )
    service._create_todoist_tasks = (  # type: ignore[method-assign]
        lambda tasks, **kwargs: created.append(tasks) or ["42"]
    )

    backfill = service.sync(
        backfill=True,
        allow_retro_todo=False,
        refresh_qmd=False,
    )
    state_after_backfill = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    incremental = service.sync(
        backfill=False,
        allow_retro_todo=True,
        refresh_qmd=False,
    )
    final_state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert backfill["tasks_created"] == 0
    assert state_after_backfill["recordings"]["file-3"]["owner_eval_pending"] is True
    assert incremental["tasks_created"] == 1
    assert final_state["recordings"]["file-3"]["owner_eval_pending"] is False
    assert final_state["recordings"]["file-3"]["task_ids"] == ["42"]


def test_plaud_sync_retries_after_todoist_creation_failure(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-4"}]
    details = {
        "file-4": {
            "file_id": "file-4",
            "title": "Recent meeting",
            "record_time": timestamp_ms,
            "trans_result": "Иванов делает follow-up.",
            "ai_content": {"summary": "Иванов делает follow-up."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "meeting",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить follow-up",
                    "due_hint": "",
                    "priority": 2,
                    "evidence": "Иванов делает follow-up.",
                }
            ],
            "search_value": {"topics": [], "entities": [], "meeting_prep_value": True},
        },
        ensure_ascii=False,
    )
    calls: list[list[dict[str, object]]] = []
    results = iter([[], ["77"]])
    service._create_todoist_tasks = (  # type: ignore[method-assign]
        lambda tasks, **kwargs: calls.append(tasks) or next(results)
    )

    first = service.sync(backfill=True, refresh_qmd=False)
    first_state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    second = service.sync(backfill=False, refresh_qmd=False)
    second_state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert first["tasks_created"] == 0
    assert first_state["recordings"]["file-4"]["owner_eval_pending"] is True
    assert first_state["recordings"]["file-4"]["todoist_fingerprint"] == ""
    assert second["tasks_created"] == 1
    assert second_state["recordings"]["file-4"]["owner_eval_pending"] is False
    assert second_state["recordings"]["file-4"]["task_ids"] == ["77"]
    assert len(calls) == 2


def test_plaud_sync_persists_pending_before_todoist_call(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-pending-order"}]
    details = {
        "file-pending-order": {
            "file_id": "file-pending-order",
            "title": "Pending order",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить письмо.",
            "ai_content": {"summary": "Отправить письмо."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "meeting",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить письмо",
                    "due_hint": "",
                    "priority": 2,
                    "evidence": "Надо отправить письмо.",
                }
            ],
        },
        ensure_ascii=False,
    )
    pending_at_call: list[str] = []

    def fake_create(tasks, **kwargs):  # type: ignore[no-untyped-def]
        del tasks, kwargs
        state = json.loads(
            (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
        )
        pending_at_call.append(
            state["recordings"]["file-pending-order"]["todoist_pending_fingerprint"]
        )
        return ["91"]

    service._create_todoist_tasks = fake_create  # type: ignore[method-assign]

    result = service.sync(backfill=True, refresh_qmd=False)
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    recording_state = state["recordings"]["file-pending-order"]

    assert result["tasks_created"] == 1
    assert len(pending_at_call) == 1
    assert pending_at_call[0]
    assert "todoist_pending_fingerprint" not in recording_state
    assert recording_state["task_ids"] == ["91"]


def test_plaud_sync_does_not_retry_after_crash_between_todoist_and_state_commit(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-crash"}]
    details = {
        "file-crash": {
            "file_id": "file-crash",
            "title": "Crash window",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить follow-up.",
            "ai_content": {"summary": "Отправить follow-up."},
        }
    }

    def verdict(prompt: str) -> str:
        del prompt
        return json.dumps(
            {
                "context_type": "meeting",
                "archive": True,
                "todoist_create": True,
                "owner_confidence": "high",
                "reason": "owner task",
                "tasks": [
                    {
                        "content": "Отправить follow-up",
                        "due_hint": "",
                        "priority": 2,
                        "evidence": "Надо отправить follow-up.",
                    }
                ],
            },
            ensure_ascii=False,
        )

    todoist_calls = {"count": 0}
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = verdict  # type: ignore[method-assign]

    def fake_create(tasks, **kwargs):  # type: ignore[no-untyped-def]
        del tasks, kwargs
        todoist_calls["count"] += 1
        return ["501"]

    service._create_todoist_tasks = fake_create  # type: ignore[method-assign]
    original_save_state = service._save_state
    save_calls = {"count": 0}

    def crash_on_second_save(payload):  # type: ignore[no-untyped-def]
        save_calls["count"] += 1
        if save_calls["count"] == 2:
            raise KeyboardInterrupt("crash before confirmed task state")
        original_save_state(payload)

    service._save_state = crash_on_second_save  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        service.sync(backfill=True, refresh_qmd=False)

    crashed_state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    assert crashed_state["recordings"]["file-crash"]["todoist_pending_fingerprint"]

    resumed_service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    resumed_service._run_prompt = verdict  # type: ignore[method-assign]
    resumed_service._create_todoist_tasks = fake_create  # type: ignore[method-assign]

    resumed = resumed_service.sync(backfill=True, refresh_qmd=False)
    resumed_state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )["recordings"]["file-crash"]

    assert resumed["tasks_created"] == 0
    assert todoist_calls["count"] == 1
    assert resumed_state["todoist_pending_fingerprint"]
    assert resumed_state["owner_eval_pending"] is True


@pytest.mark.parametrize("first_outcome", ["exception", "explicit-failure"])
def test_plaud_todoist_rollback_remains_retryable_if_final_save_crashes(
    tmp_path: Path,
    first_outcome: str,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-rollback"}]
    details = {
        "file-rollback": {
            "file_id": "file-rollback",
            "title": "Rollback state",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить follow-up.",
            "ai_content": {"summary": "Отправить follow-up."},
        }
    }
    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "recordings": {
                    "file-rollback": {
                        "status": "imported",
                        "owner_eval_pending": False,
                        "task_ids": [],
                        "todoist_fingerprint": "",
                    }
                },
                "dirty_weeks": [],
                "last_sync_at": None,
            }
        ),
        encoding="utf-8",
    )

    def verdict(prompt: str) -> str:
        del prompt
        return json.dumps(
            {
                "context_type": "meeting",
                "archive": True,
                "todoist_create": True,
                "owner_confidence": "high",
                "reason": "owner task",
                "tasks": [
                    {
                        "content": "Отправить follow-up",
                        "due_hint": "",
                        "priority": 2,
                        "evidence": "Надо отправить follow-up.",
                    }
                ],
            },
            ensure_ascii=False,
        )

    todoist_calls = {"count": 0}

    def fake_create(tasks, **kwargs):  # type: ignore[no-untyped-def]
        del tasks, kwargs
        todoist_calls["count"] += 1
        if todoist_calls["count"] == 1:
            if first_outcome == "exception":
                raise RuntimeError("local Todoist invocation failed")
            return []
        return ["701"]

    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = verdict  # type: ignore[method-assign]
    service._create_todoist_tasks = fake_create  # type: ignore[method-assign]
    original_save_state = service._save_state
    save_calls = {"count": 0}

    def crash_on_third_save(payload):  # type: ignore[no-untyped-def]
        save_calls["count"] += 1
        if save_calls["count"] == 3:
            raise KeyboardInterrupt("crash after rollback state")
        original_save_state(payload)

    service._save_state = crash_on_third_save  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        service.sync(backfill=True, refresh_qmd=False)

    rolled_back_state = json.loads(state_path.read_text(encoding="utf-8"))[
        "recordings"
    ]["file-rollback"]
    assert rolled_back_state["owner_eval_pending"] is True
    assert "todoist_pending_fingerprint" not in rolled_back_state

    resumed_service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    resumed_service._run_prompt = verdict  # type: ignore[method-assign]
    resumed_service._create_todoist_tasks = fake_create  # type: ignore[method-assign]

    resumed = resumed_service.sync(backfill=False, refresh_qmd=False)
    resumed_state = json.loads(state_path.read_text(encoding="utf-8"))["recordings"][
        "file-rollback"
    ]

    assert resumed["tasks_created"] == 1
    assert todoist_calls["count"] == 2
    assert resumed_state["owner_eval_pending"] is False
    assert resumed_state["task_ids"] == ["701"]


@pytest.mark.parametrize("outcome", ["timeout", "malformed", "missing-task-ids"])
def test_plaud_create_todoist_tasks_returns_unknown_without_retry(
    tmp_path: Path,
    monkeypatch,
    outcome: str,
) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
    )
    service.project_catalog.get_catalog = lambda **kwargs: {  # type: ignore[method-assign]
        "available": True,
        "catalog": None,
    }
    service.project_router.route_task = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    calls = {"count": 0}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        calls["count"] += 1
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args[0], timeout=3600)
        stdout = "not json" if outcome == "malformed" else '{"tasks": []}'
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("d_brain.services.plaud.time.sleep", lambda seconds: None)

    result = service._create_todoist_tasks(
        [
            {
                "content": "Отправить follow-up",
                "due_hint": "",
                "priority": 2,
                "evidence": "Надо отправить follow-up.",
            }
        ]
    )

    assert result is None
    assert calls["count"] == 1


def test_plaud_sync_persists_created_tasks_before_processing_next_item(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path.parent / "skills/dbrain-processor/references").mkdir(parents=True)
    (vault_path.parent / "skills/dbrain-processor/references/plaud.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)
    items = [{"file_id": "file-5"}, {"file_id": "file-6"}]
    details = {
        "file-5": {
            "file_id": "file-5",
            "title": "First memo",
            "record_time": timestamp_ms,
            "trans_result": "Надо отправить follow-up.",
            "ai_content": {"summary": "Отправить follow-up."},
        },
        "file-6": {
            "file_id": "file-6",
            "title": "Second memo",
            "record_time": timestamp_ms,
            "trans_result": "Вторую запись обработать позже.",
            "ai_content": {"summary": "Вторая запись."},
        },
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "meeting",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task",
            "tasks": [
                {
                    "content": "Отправить follow-up",
                    "due_hint": "",
                    "priority": 2,
                    "evidence": "Надо отправить follow-up.",
                }
            ],
            "search_value": {"topics": [], "entities": [], "meeting_prep_value": True},
        },
        ensure_ascii=False,
    )
    service._create_todoist_tasks = lambda tasks, **kwargs: ["501"]  # type: ignore[method-assign]

    original_sync_one = service._sync_one
    calls = {"count": 0}

    def crashing_sync_one(**kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt("crash after first persisted item")
        return original_sync_one(**kwargs)

    service._sync_one = crashing_sync_one  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        service.sync(backfill=True, refresh_qmd=False)

    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert state["recordings"]["file-5"]["task_ids"] == ["501"]
    assert state["recordings"]["file-5"]["todoist_fingerprint"] != ""


def test_plaud_create_todoist_tasks_uses_mcp_priority_strings(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "project" / "vault"
    vault_path.mkdir(parents=True)
    (vault_path.parent / "mcp-config.json").write_text("{}", encoding="utf-8")

    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-token",
    )
    service.project_catalog.get_catalog = lambda **kwargs: {  # type: ignore[method-assign]
        "available": True,
        "refreshed": True,
        "catalog": {
            "fetched_at": "2026-04-04T10:00:00+00:00",
            "inbox_project_id": "inbox-id",
            "projects": [{"id": "work-id", "name": "Рабочие"}],
        },
        "errors": [],
    }
    service.project_router.route_task = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "project_id": "work-id",
        "confidence": "high",
        "reason": "work",
    }
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = args[0]
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"structuredContent":{"tasks":[{"id":"123","priority":"p2"}]},'
                '"content":[{"type":"text","text":"ok"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    created = service._create_todoist_tasks(
        [
            {
                "content": "Отправить follow-up",
                "due_hint": "tomorrow",
                "priority": 2,
                "evidence": "text",
            }
        ]
    )

    payload = json.loads(captured["command"][4])
    assert created == ["123"]
    assert payload["tasks"][0]["priority"] == "p2"
    assert payload["tasks"][0]["dueString"] == "tomorrow"
    assert payload["tasks"][0]["projectId"] == "work-id"
    assert captured["env"]["MCP_CONFIG_PATH"] == str(
        (vault_path.parent / "mcp-config.json").resolve()
    )


def test_plaud_sync_service_exposes_control_plane_workflow(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(vault_path, bearer_token="token")

    assert service.workflow_name == "integration.plaud.sync"
    service.close()
