"""Regression tests for the PLAUD audit fixes (2026-09-03, group G7).

Covers:
- A: stable content hash that ignores presigned ``data_link`` query strings,
  and redaction of any signed-URL-like field (not just ``data_link``) before
  raw JSON hits disk.
- B: ``pending_summary`` records are re-fetched on incremental syncs and,
  once stale, imported with a transcript-only placeholder note; placeholder
  imports stay eligible for re-check until a real summary arrives.
- C: ``_content_blob_to_text`` unwraps a stringified JSON blob.
- D: Todoist task creation is deduplicated by stored ``task_ids`` alone.
- E: ``_recorded_at`` reuses a persisted fallback instead of ``now()``, both
  during sync and during ``migrate_note_path_drift``.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import _write_vault_manifest

from d_brain.services.plaud import (
    PlaudSyncService,
    _content_blob_to_text,
    _redact_detail_for_storage,
    _stable_detail_view,
)


class _FakeClient:
    """Minimal fake PLAUD client, following the pattern in test_plaud_service.py."""

    def __init__(
        self,
        items: list[dict[str, object]],
        details: dict[str, dict[str, object]],
        *,
        get_recording_calls: list[str] | None = None,
    ) -> None:
        self.items = items
        self.details = details
        self.get_recording_calls = (
            get_recording_calls if get_recording_calls is not None else []
        )

    def iter_recordings(self, *, limit: int = 100, max_pages: int | None = None):  # type: ignore[no-untyped-def]
        del limit, max_pages
        yield from self.items

    def get_recording(self, file_id: str) -> dict[str, object]:
        self.get_recording_calls.append(file_id)
        return self.details[file_id]


@pytest.fixture(autouse=True)
def _plaud_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


def _write_reference(vault_path: Path) -> None:
    ref_dir = vault_path.parent / "skills/dbrain-processor/references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "plaud.md").write_text("owner={OWNER_FULL_NAME}", encoding="utf-8")


def _archive_only_verdict(prompt: str) -> str:
    del prompt
    return json.dumps(
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": False,
            "owner_confidence": "none",
            "reason": "archive only",
            "tasks": [],
        },
        ensure_ascii=False,
    )


# --- Task A: stable content hash + raw redaction -----------------------


def test_stable_detail_view_ignores_signed_data_link_query() -> None:
    base = {
        "file_id": "f1",
        "title": "T",
        "record_time": 123,
        "trans_result": "transcript text",
        "ai_content": "summary text",
        "content_list": [
            {
                "data_id": "1",
                "data_type": "auto_sum_note",
                "data_link": (
                    "https://s3.example.com/abc"
                    "?X-Amz-Signature=aaa&X-Amz-Date=today"
                ),
            }
        ],
    }
    rotated = {
        **base,
        "content_list": [
            {
                "data_id": "1",
                "data_type": "auto_sum_note",
                "data_link": (
                    "https://s3.example.com/abc"
                    "?X-Amz-Signature=bbb&X-Amz-Date=tomorrow"
                ),
            }
        ],
    }

    assert _stable_detail_view(base) == _stable_detail_view(rotated)


def test_sync_reports_unchanged_despite_rotated_signed_data_link(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    now = datetime.now().astimezone()
    timestamp_ms = int(now.timestamp() * 1000)

    def detail_with_link(query: str) -> dict[str, object]:
        return {
            "file_id": "file-rotate",
            "title": "Rotating link",
            "record_time": timestamp_ms,
            "trans_result": "Транскрипт не меняется.",
            "ai_content": {"summary": "Саммари не меняется."},
            "content_list": [
                {
                    "data_id": "1",
                    "data_type": "auto_sum_note",
                    "data_link": f"https://s3.example.com/blob?{query}",
                }
            ],
        }

    items = [{"file_id": "file-rotate"}]
    client = _FakeClient(
        items, {"file-rotate": detail_with_link("X-Amz-Signature=first")}
    )
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=client,
    )
    service._run_prompt = _archive_only_verdict  # type: ignore[method-assign]

    first = service.sync(backfill=True, refresh_qmd=False)
    client.details["file-rotate"] = detail_with_link("X-Amz-Signature=second")
    second = service.sync(backfill=True, refresh_qmd=False)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["unchanged"] == 1


def test_raw_json_redacts_signed_data_link_query(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    now = datetime.now().astimezone()
    items = [{"file_id": "file-secret"}]
    details = {
        "file-secret": {
            "file_id": "file-secret",
            "title": "Secret link",
            "record_time": int(now.timestamp() * 1000),
            "trans_result": "Транскрипт.",
            "ai_content": {"summary": "Саммари."},
            "content_list": [
                {
                    "data_id": "1",
                    "data_type": "auto_sum_note",
                    "data_link": (
                        "https://s3.example.com/blob/path"
                        "?X-Amz-Signature=super-secret&X-Amz-Security-Token=tok"
                    ),
                }
            ],
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakeClient(items, details),
    )
    service._run_prompt = _archive_only_verdict  # type: ignore[method-assign]

    service.sync(backfill=True, refresh_qmd=False)

    raw_files = list((vault_path / "imports" / "plaud" / "raw").rglob("*.json"))
    assert len(raw_files) == 1
    raw_content = raw_files[0].read_text(encoding="utf-8")
    assert "X-Amz-Signature" not in raw_content
    assert "X-Amz-Security-Token" not in raw_content
    assert "https://s3.example.com/blob/path" in raw_content


def test_redact_detail_for_storage_keeps_non_link_fields() -> None:
    detail = {
        "file_id": "f1",
        "content_list": [
            {"data_id": "1", "data_link": "https://x/y?sig=abc"},
        ],
    }
    redacted = _redact_detail_for_storage(detail)
    assert redacted["file_id"] == "f1"
    assert redacted["content_list"][0]["data_link"] == "https://x/y"
    assert redacted["content_list"][0]["data_id"] == "1"


def test_redact_detail_for_storage_redacts_unknown_signed_url_key() -> None:
    """Any ``*_url``/``*_link`` field, not just ``data_link``, must be redacted.

    PLAUD could expose the same presigned-S3 pattern under a field name we
    have not seen yet (e.g. ``audio_url``); catching it by key suffix (or by
    the ``X-Amz-`` marker in the query) avoids depending on an exhaustive
    field allowlist.
    """
    detail = {
        "file_id": "f1",
        "audio_url": (
            "https://cdn.example.com/a.mp3"
            "?X-Amz-Signature=zzz&X-Amz-Date=today"
        ),
    }

    redacted = _redact_detail_for_storage(detail)

    assert redacted["audio_url"] == "https://cdn.example.com/a.mp3"
    assert "X-Amz-Signature" not in redacted["audio_url"]


# --- Task B: pending_summary re-fetch + stale placeholder import -------


def test_incremental_sync_rechecks_stale_pending_summary_off_page(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    now = datetime.now().astimezone()
    old_time = now - timedelta(days=3)

    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "recordings": {
                    "file-old": {
                        "status": "pending_summary",
                        "content_hash": "stale-hash",
                        "recorded_at": old_time.isoformat(),
                        "first_seen_at": old_time.isoformat(),
                    }
                },
                "dirty_weeks": [],
                "last_sync_at": None,
            }
        ),
        encoding="utf-8",
    )

    # Only the newest recording appears on the fetched page; ``file-old`` has
    # fallen off it, which is exactly the paging gap that stranded pending
    # summaries before this fix.
    items = [{"file_id": "file-new"}]
    details = {
        "file-new": {
            "file_id": "file-new",
            "title": "New memo",
            "record_time": int(now.timestamp() * 1000),
            "trans_result": "Новая запись.",
            "ai_content": {"summary": "Саммари новой записи."},
        },
        "file-old": {
            "file_id": "file-old",
            "title": "Old memo, summary finally arrived",
            "record_time": int(old_time.timestamp() * 1000),
            "trans_result": "Старая запись.",
            "ai_content": {"summary": "Саммари наконец пришло."},
        },
    }
    calls: list[str] = []
    client = _FakeClient(items, details, get_recording_calls=calls)
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=client,
    )
    service._run_prompt = _archive_only_verdict  # type: ignore[method-assign]

    result = service.sync(backfill=False, refresh_qmd=False)

    assert "file-old" in calls
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["recordings"]["file-old"]["status"] == "imported"
    assert result["imported"] == 2


def test_pending_summary_resync_is_bounded(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    now = datetime.now().astimezone()

    recordings = {}
    details: dict[str, dict[str, object]] = {}
    for index in range(55):
        file_id = f"file-pending-{index:02d}"
        seen_at = now - timedelta(days=55 - index)
        recordings[file_id] = {
            "status": "pending_summary",
            "content_hash": f"hash-{index}",
            "recorded_at": seen_at.isoformat(),
            "first_seen_at": seen_at.isoformat(),
        }
        details[file_id] = {
            "file_id": file_id,
            "title": f"Memo {index}",
            "record_time": int(seen_at.timestamp() * 1000),
            "trans_result": "",
            "ai_content": "",
        }

    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {"recordings": recordings, "dirty_weeks": [], "last_sync_at": None}
        ),
        encoding="utf-8",
    )

    calls: list[str] = []
    client = _FakeClient([], details, get_recording_calls=calls)
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=client,
    )

    service.sync(backfill=False, refresh_qmd=False)

    assert len(calls) == 50
    # Oldest (lowest index) records are checked first.
    assert calls[0] == "file-pending-00"
    assert "file-pending-54" not in calls


def test_pending_summary_gives_up_on_records_past_give_up_age(
    tmp_path: Path,
) -> None:
    """Records past the give-up age never crowd out newer ones.

    Without a ceiling, 50+ recordings whose summary never arrives (or whose
    ``get_recording`` call permanently fails because PLAUD deleted the file)
    would occupy every slot in the bounded resync pass forever, and a newer
    pending/placeholder record would never be re-checked at all.
    """
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()

    recordings: dict[str, dict[str, object]] = {}
    for index in range(60):
        seen_at = now - timedelta(days=90 + index)
        recordings[f"file-dead-{index:02d}"] = {
            "status": "pending_summary",
            "content_hash": f"hash-{index}",
            "recorded_at": seen_at.isoformat(),
            "first_seen_at": seen_at.isoformat(),
        }
    fresh_seen_at = now - timedelta(days=5)
    recordings["file-fresh"] = {
        "status": "pending_summary",
        "content_hash": "hash-fresh",
        "recorded_at": fresh_seen_at.isoformat(),
        "first_seen_at": fresh_seen_at.isoformat(),
    }

    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakeClient([], {}),
    )

    selected = service._pending_summary_file_ids(
        {"recordings": recordings, "dirty_weeks": [], "last_sync_at": None}
    )

    assert selected == ["file-fresh"]


def test_pending_summary_older_than_max_age_imports_transcript_placeholder(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    old_time = datetime.now().astimezone() - timedelta(days=10)
    items = [{"file_id": "file-stale"}]
    details = {
        "file-stale": {
            "file_id": "file-stale",
            "title": "Stale memo",
            "record_time": int(old_time.timestamp() * 1000),
            "trans_result": "Транскрипт есть, саммари так и не появилось.",
            "ai_content": "",
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakeClient(items, details),
    )

    result = service.sync(backfill=True, refresh_qmd=False)

    assert result["imported"] == 1
    assert result["pending_summary"] == 0
    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    assert len(note_files) == 1
    note_content = note_files[0].read_text(encoding="utf-8")
    assert "Саммари недоступно, импортирован транскрипт" in note_content
    assert "Транскрипт есть, саммари так и не появилось." in note_content
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    assert state["recordings"]["file-stale"]["status"] == "imported"


def test_placeholder_import_is_rechecked_and_updated_once_summary_arrives(
    tmp_path: Path,
) -> None:
    """A transcript-only placeholder import must not be a dead end.

    Once ``ai_content`` finally shows up, a normal (non-backfill) sync has to
    notice it via the same bounded pending re-check pass used for
    ``pending_summary`` records, rewrite the note with the real summary, and
    clear the ``summary_placeholder`` flag.
    """
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    old_time = datetime.now().astimezone() - timedelta(days=10)
    items = [{"file_id": "file-placeholder"}]
    details = {
        "file-placeholder": {
            "file_id": "file-placeholder",
            "title": "Placeholder memo",
            "record_time": int(old_time.timestamp() * 1000),
            "trans_result": "Транскрипт есть, саммари пока нет.",
            "ai_content": "",
        }
    }
    client = _FakeClient(items, details)
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=client,
    )

    first = service.sync(backfill=True, refresh_qmd=False)
    assert first["imported"] == 1
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    assert state["recordings"]["file-placeholder"]["summary_placeholder"] is True

    # The summary finally arrives, but the record has aged off the fetched
    # page (as it eventually would) -- only the bounded pending re-check
    # pass can still reach it.
    client.items = []
    client.details["file-placeholder"] = {
        **details["file-placeholder"],
        "ai_content": {"summary": "Саммари наконец пришло."},
    }

    second = service.sync(backfill=False, refresh_qmd=False)

    assert second["imported"] == 1
    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    assert len(note_files) == 1
    note_content = note_files[0].read_text(encoding="utf-8")
    assert "Саммари наконец пришло." in note_content
    assert "Саммари недоступно, импортирован транскрипт" not in note_content
    state = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )
    assert state["recordings"]["file-placeholder"]["summary_placeholder"] is False


def test_pending_summary_recent_without_transcript_stays_pending(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime.now().astimezone()
    items = [{"file_id": "file-fresh-pending"}]
    details = {
        "file-fresh-pending": {
            "file_id": "file-fresh-pending",
            "title": "Fresh pending memo",
            "record_time": int(now.timestamp() * 1000),
            "trans_result": "",
            "ai_content": "",
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakeClient(items, details),
    )

    result = service.sync(backfill=True, refresh_qmd=False)

    assert result["pending_summary"] == 1
    assert result["imported"] == 0
    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    assert note_files == []


# --- Task C: stringified JSON content blob ------------------------------


def test_content_blob_to_text_extracts_from_json_string() -> None:
    blob = '{"ai_content": "хочу перезвонить клиенту", "category": "note"}'

    assert _content_blob_to_text(blob) == "хочу перезвонить клиенту"


def test_content_blob_to_text_falls_back_to_raw_string_on_bad_json() -> None:
    blob = "{not valid json"

    assert _content_blob_to_text(blob) == "{not valid json"


def test_content_blob_to_text_handles_json_array_string() -> None:
    blob = '[{"topic": "первый пункт"}, {"topic": "второй пункт"}]'

    result = _content_blob_to_text(blob)

    assert "первый пункт" in result
    assert "второй пункт" in result


def test_content_blob_to_text_handles_empty_string() -> None:
    """An empty/blank string must not crash the ``stripped[:1] in "{["`` check."""
    assert _content_blob_to_text("") == ""
    assert _content_blob_to_text("   ") == ""


# --- Task D: Todoist dedup by task_ids alone -----------------------------


def test_todoist_dedup_skips_recreation_despite_fingerprint_drift(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    now = datetime.now().astimezone()
    items = [{"file_id": "file-dedup"}]
    details = {
        "file-dedup": {
            "file_id": "file-dedup",
            "title": "Dedup check",
            "record_time": int(now.timestamp() * 1000),
            "trans_result": "Слегка другой транскрипт для отличия хэша.",
            "ai_content": {"summary": "Итог."},
        }
    }
    state_path = vault_path / ".sync" / "plaud-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "recordings": {
                    "file-dedup": {
                        "status": "imported",
                        "content_hash": "outdated-hash",
                        "owner_eval_pending": False,
                        "task_ids": ["999"],
                        "todoist_fingerprint": "old-fingerprint-not-matching",
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
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
        client=_FakeClient(items, details),
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "meeting",
            "archive": True,
            "todoist_create": True,
            "owner_confidence": "high",
            "reason": "owner task, wording differs from the previous run",
            "tasks": [
                {
                    "content": "Что-то сделать",
                    "due_hint": "",
                    "priority": 2,
                    "evidence": "новая формулировка отличается от прошлой",
                }
            ],
        },
        ensure_ascii=False,
    )
    create_calls: list[list[dict[str, object]]] = []
    service._create_todoist_tasks = (  # type: ignore[method-assign]
        lambda tasks, **kwargs: create_calls.append(tasks) or ["should-not-be-used"]
    )

    result = service.sync(backfill=True, refresh_qmd=False)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert create_calls == []
    assert state["recordings"]["file-dedup"]["task_ids"] == ["999"]
    assert result["tasks_created"] == 0


# --- Task E: recorded_at fallback stability ------------------------------


def test_recorded_at_reuses_fallback_when_timestamps_missing(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakeClient([], {}),
    )
    fallback = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)

    reused = service._recorded_at({}, fallback=fallback)
    fresh = service._recorded_at({})

    assert reused == fallback.astimezone()
    assert fresh != fallback.astimezone()


def test_sync_keeps_stable_note_path_when_all_timestamps_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    items = [{"file_id": "file-no-ts"}]
    details = {
        "file-no-ts": {
            "file_id": "file-no-ts",
            "title": "No timestamp memo",
            "trans_result": "Транскрипт без времени записи.",
            "ai_content": {"summary": "Саммари без времени записи."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakeClient(items, details),
    )
    service._run_prompt = _archive_only_verdict  # type: ignore[method-assign]

    now_holder = {"value": datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(
        "d_brain.services.plaud._utc_now", lambda: now_holder["value"]
    )

    service.sync(backfill=True, refresh_qmd=False)
    notes_after_first = sorted(
        (vault_path / "imports" / "plaud" / "notes").rglob("*.md")
    )
    state_after_first = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    now_holder["value"] = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    service.sync(backfill=True, refresh_qmd=False)
    notes_after_second = sorted(
        (vault_path / "imports" / "plaud" / "notes").rglob("*.md")
    )
    state_after_second = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert len(notes_after_first) == 1
    assert notes_after_first == notes_after_second
    assert (
        state_after_first["recordings"]["file-no-ts"]["recorded_at"]
        == state_after_second["recordings"]["file-no-ts"]["recorded_at"]
    )


def test_migrate_note_path_drift_is_idempotent_without_timestamps(
    tmp_path: Path,
) -> None:
    """Repeated ``migrate_note_path_drift`` must not keep moving a note.

    Without any timestamp field in the raw detail, ``_recorded_at`` used to
    fall back to a fresh ``now()`` on every call, so the "canonical" note
    path kept changing and the note would be moved again on every run.
    """
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    detail = {
        "file_id": "file-no-time",
        "title": "No timestamp memo",
        "trans_result": "Транскрипт.",
        "ai_content": {"summary": "Саммари."},
    }
    raw_path = (
        vault_path
        / "imports"
        / "plaud"
        / "raw"
        / "2025"
        / "01"
        / "file-no-time.json"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    # The note starts at a non-canonical path, so the first migration has to
    # move it once.
    note_path = (
        vault_path / "imports" / "plaud" / "notes" / "misc" / "file-no-time.md"
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\n"
        "type: plaud-recording\n"
        "source: plaud\n"
        "source_id: file-no-time\n"
        "raw_path: imports/plaud/raw/2025/01/file-no-time.json\n"
        "last_accessed: 2026-04-30\n"
        "relevance: 0.5\n"
        "tier: warm\n"
        "---\n\n"
        "# No timestamp memo\n",
        encoding="utf-8",
    )

    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        owner_full_name="Иван Иванов",
        client=_FakeClient([], {}),
    )

    first = service.migrate_note_path_drift(refresh_qmd=False)
    notes_after_first = sorted(
        (vault_path / "imports" / "plaud" / "notes").rglob("*.md")
    )
    state_after_first = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    second = service.migrate_note_path_drift(refresh_qmd=False)
    notes_after_second = sorted(
        (vault_path / "imports" / "plaud" / "notes").rglob("*.md")
    )
    state_after_second = json.loads(
        (vault_path / ".sync" / "plaud-state.json").read_text(encoding="utf-8")
    )

    assert first["migrated"] == 1
    assert second["migrated"] == 0
    assert notes_after_first == notes_after_second
    assert len(notes_after_first) == 1
    assert (
        state_after_first["recordings"]["file-no-time"]["recorded_at"]
        == state_after_second["recordings"]["file-no-time"]["recorded_at"]
    )


# --- Todoist call gets the allowlisted environment ---------------------------


def test_create_todoist_tasks_env_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault_path = tmp_path / "vault"
    _write_reference(vault_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        todoist_api_key="todoist-secret",
        owner_full_name="Иван Иванов",
        client=_FakeClient([], {}),
    )
    service.project_catalog.get_catalog = (  # type: ignore[method-assign]
        lambda *, force_refresh=False: {
            "available": False,
            "catalog": None,
            "errors": [],
        }
    )
    captured: dict[str, str] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 0, '{"tasks": [{"id": "1"}]}', "")

    monkeypatch.setattr("d_brain.services.plaud.subprocess.run", fake_run)

    created = service._create_todoist_tasks(
        [{"content": "Купить молоко", "priority": 2, "due_hint": ""}]
    )

    assert created == ["1"]
    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["TODOIST_API_KEY"] == "todoist-secret"
    assert captured["PATH"] == "/usr/bin"
