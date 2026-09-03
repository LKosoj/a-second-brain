"""Regression tests for the G12 "compiled" audit fixes in
``d_brain.services.compiled_briefings``:

A) double adjudication -- a conflict pair on a page must be adjudicated once
   per ``_upsert_briefing`` call, not once per internal render.
B) ``_drain_queue_once`` must retry ``ai-cli-unavailable`` like any other
   transient error instead of dropping the source after one attempt.
C) a batch drain must advance the worker heartbeat after each event it
   processes, not just once per ``run_queue_worker`` loop iteration.
D) rollback correctness: a page written twice in one pass must still roll
   back (not be reported "skipped"), and rollback must also restore the
   ``source-state.json`` chunk-hash ledger for pages it restores or deletes.
E) the nightly conflict retry must recover a daily source's real trust
   level instead of always rating it "inferred" for lack of an excerpt.

Reuses the harness helpers from ``test_compiled_briefings.py`` (same
vault-builder/model-stub conventions) rather than duplicating them.
"""

import json
import os
from pathlib import Path

import pytest
from test_compiled_briefings import (
    _bypass_atomic_vault_write,
    _compiled_service,
    _conflict_page_on_disk,
    _demo_target,
    _minimal_compile_payload,
    _stub_adjudicator,
)

from d_brain.run_compiled_pass import _run_rollback
from d_brain.services.compiled_briefings import CompileEnrichPass

# --- A) double adjudication ------------------------------------------------


def test_upsert_briefing_adjudicates_each_conflict_pair_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_upsert_briefing`` renders the page twice internally: once via
    ``_extract_and_verify_claims`` to build Verify's candidate markdown, and
    once for the real write. Before the fix, a page with two open conflicts
    burned four adjudicator calls (2 conflicts x 2 renders) instead of two,
    and the final render's (non-deterministic) verdict could disagree with
    the one Verify had already approved."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)

    existing_text = (
        "---\ndomain: projects\n---\n\n# Demo Project\n\n"
        "## Sources That Shaped This Page\n"
        "| Date | Source | What Added |\n"
        "| --- | --- | --- |\n"
        "| 2026-07-01 | [[thoughts/idea-a.md]] | Дедлайн А — 1 сентября. |\n"
        "| 2026-07-01 | [[thoughts/idea-b.md]] | Бюджет Б — 100000. |\n"
    )
    page_path = vault_path / "compiled" / "projects" / "demo-project.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(existing_text, encoding="utf-8")

    raw_claims = [
        {"text": "Дедлайн А — 15 сентября.", "kind": "fact"},
        {"text": "Бюджет Б — 200000.", "kind": "fact"},
    ]
    normalized = service._normalize_claims(
        raw_claims, source_rel_path="daily/2026-08-05.md"
    )
    compile_payload = _minimal_compile_payload(
        claims=raw_claims,
        conflicts=[
            {
                "existing_claim": "Дедлайн А — 1 сентября.",
                "existing_source": "thoughts/idea-a.md",
                "new_claim": "Дедлайн А — 15 сентября.",
                "type": "factual",
            },
            {
                "existing_claim": "Бюджет Б — 100000.",
                "existing_source": "thoughts/idea-b.md",
                "new_claim": "Бюджет Б — 200000.",
                "type": "factual",
            },
        ],
    )
    verify_payload = {
        "verdicts": [
            {
                "index": index,
                "text": claim["text"],
                "supported": True,
                "reason": "stated in source",
            }
            for index, claim in enumerate(normalized)
        ],
        "page_checks": {
            "source_coverage": True,
            "target_scope": True,
            "timeline_consistency": True,
        },
        "page_issues": [],
    }
    responses = [json.dumps(compile_payload), json.dumps(verify_payload)]
    calls: list[str] = []

    def fake_run(prompt: str, *, timeout: int) -> str:
        calls.append(prompt)
        return responses[len(calls) - 1]

    monkeypatch.setattr(service.runner, "run", fake_run)
    asked = _stub_adjudicator(monkeypatch, service, ("both_valid", "разные периоды"))

    result = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-05.md",
        source_excerpt=(
            "## 09:00 [text]\nДедлайн А — 15 сентября. Бюджет Б — 200000."
        ),
        signal=None,
    )

    assert result.written is True
    # Exactly one model call per distinct conflict pair, not one per
    # (pair x render).
    assert len(asked) == 2
    pairs = {(call["existing_claim"], call["new_claim"]) for call in asked}
    assert pairs == {
        ("Дедлайн А — 1 сентября.", "Дедлайн А — 15 сентября."),
        ("Бюджет Б — 100000.", "Бюджет Б — 200000."),
    }


# --- B) ai-cli-unavailable must be retried, not dropped on first attempt --


def test_drain_queue_once_retries_ai_cli_unavailable_before_dropping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A few minutes of CLI downtime must not permanently drop a queued
    source: ``ai-cli-unavailable`` has to go through the same attempts<3
    backoff path as any other transient error, exactly like
    ``backend-refused`` does (see
    ``test_compiled_briefings_drain_records_a_source_it_permanently_gave_up_on``
    in ``test_compiled_briefings.py``), and only be dropped once that
    budget is exhausted."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="body",
        debounce_seconds=0,
    )
    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(
        service,
        "refresh_after_write",
        lambda **kwargs: {"updated": [], "errors": ["ai-cli-unavailable"]},
    )

    def _queue() -> list[dict[str, object]]:
        return json.loads(
            (vault_path / ".compiled" / "queue.json").read_text(encoding="utf-8")
        )

    service._drain_queue_once(force=True, max_events=50)
    queue = _queue()
    assert len(queue) == 1
    assert queue[0]["state"] == "pending"
    assert queue[0]["attempts"] == 1

    service._drain_queue_once(force=True, max_events=50)
    queue = _queue()
    assert len(queue) == 1
    assert queue[0]["attempts"] == 2

    service._drain_queue_once(force=True, max_events=50)
    assert _queue() == []
    journal = json.loads(
        (vault_path / ".session" / "compile-dropped-sources.json").read_text(
            encoding="utf-8"
        )
    )
    assert [entry["source_path"] for entry in journal["sources"]] == [
        "daily/2026-04-04.md"
    ]
    assert journal["sources"][0]["attempts"] == 3
    assert journal["sources"][0]["errors"] == ["ai-cli-unavailable"]


# --- C) heartbeat must advance per event, not just per loop iteration -----


def test_drain_queue_once_touches_heartbeat_after_each_event(
    tmp_path: Path,
) -> None:
    """A batch of several queued sources, each worth a multi-minute model
    timeout, can take much longer than the worker-state staleness window
    (see ``_worker_state_is_live``) to finish. Before the fix, the
    heartbeat only advanced once per ``run_queue_worker`` loop iteration --
    i.e. once per whole batch, not once per event -- so
    ``spawn_background_drain`` could see a stale heartbeat mid-batch and
    wipe the worker's state out from under it."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    service.enqueue_refresh(
        source_path="daily/2026-08-05.md", source_excerpt="one", debounce_seconds=0
    )
    service.enqueue_refresh(
        source_path="daily/2026-08-06.md", source_excerpt="two", debounce_seconds=0
    )
    monkeypatch_result = {"updated": [], "errors": []}
    service.refresh_after_write = lambda **kwargs: monkeypatch_result  # type: ignore[method-assign]

    fake_clock = iter(["2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"])
    touches: list[str] = []

    def fake_touch() -> None:
        stamp = next(fake_clock)
        touches.append(stamp)
        service._save_worker_state(
            {
                "pid": os.getpid(),
                "status": "running",
                "started_at": stamp,
                "heartbeat_at": stamp,
            }
        )

    result = service._drain_queue_once(
        force=True, max_events=50, on_event_processed=fake_touch
    )

    assert result["drained"] == 2
    # Called once per event, with the fake clock advancing each time --
    # proof the callback fires per event rather than once for the batch.
    assert touches == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:05:00+00:00",
    ]
    state = json.loads(
        (vault_path / ".compiled" / "worker-state.json").read_text(encoding="utf-8")
    )
    assert state["heartbeat_at"] == "2026-01-01T00:05:00+00:00"


def test_run_queue_worker_touches_heartbeat_per_event_not_just_per_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration check for the wiring: ``run_queue_worker`` must pass its
    heartbeat writer down into ``_drain_queue_once`` as
    ``on_event_processed``, not only call it once at the top of its own
    loop."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    monkeypatch.setattr(service, "is_available", lambda: True)
    service.enqueue_refresh(
        source_path="daily/2026-08-05.md", source_excerpt="one", debounce_seconds=0
    )
    service.enqueue_refresh(
        source_path="daily/2026-08-06.md", source_excerpt="two", debounce_seconds=0
    )
    monkeypatch.setattr(
        service,
        "refresh_after_write",
        lambda **kwargs: {"updated": [], "errors": []},
    )

    real_touch = service._touch_worker_state
    touch_calls: list[int] = []

    def spy_touch(pid: int) -> None:
        touch_calls.append(pid)
        real_touch(pid)

    monkeypatch.setattr(service, "_touch_worker_state", spy_touch)

    result = service.run_queue_worker(idle_seconds=0, poll_seconds=0.01)

    assert result["drained"] == 2
    # One touch at the top of the single loop iteration this run needs,
    # plus one more per event processed inside that iteration's drain.
    assert len(touch_calls) == 3


# --- D1) a page written twice in one pass must still roll back -----------


def test_snapshot_pass_page_refreshes_fingerprint_after_on_repeat_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fix, ``_snapshot_pass_page`` returned early on a repeat
    call for the same page and never updated ``fingerprint_after``, so a
    page enriched twice in one pass kept the FIRST write's fingerprint on
    record. Rollback's "did anything change since the pass wrote it" check
    then always found a mismatch against the page's actual (second-write)
    bytes and reported the page ``skipped`` instead of rolling it back."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)

    page_path = vault_path / "compiled" / "projects" / "demo-project.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    original_text = (
        "---\ndomain: projects\n---\n\n# Demo Project\n\n"
        "## Sources\n- [[daily/2026-08-01.md]]\n"
    )
    page_path.write_text(original_text, encoding="utf-8")

    service._active_pass = CompileEnrichPass(pass_id="pass-d1", snapshot_enabled=True)
    monkeypatch.setattr(
        service.runner, "run", lambda *a, **k: json.dumps(_minimal_compile_payload())
    )

    first = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-05.md",
        source_excerpt="First update.",
        signal=None,
    )
    assert first.written is True

    second = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-06.md",
        source_excerpt="Second update.",
        signal=None,
    )
    assert second.written is True
    assert first.path == second.path

    after_second_bytes = page_path.read_bytes()
    manifest = service._read_pass_snapshot_manifest("pass-d1")
    entry = manifest[first.path]
    assert entry["existed"] is True
    assert entry["fingerprint_before"] == service._full_content_fingerprint(
        original_text.encode("utf-8")
    ).hex()
    # The recorded "after" must track the SECOND (latest) write, not the
    # first one -- this is the exact defect the fix addresses.
    assert entry["fingerprint_after"] == service._full_content_fingerprint(
        after_second_bytes
    ).hex()

    rollback = service.rollback_compile_enrich_pass("pass-d1")

    assert rollback["restored"] == [first.path]
    assert rollback["skipped"] == []
    assert page_path.read_text(encoding="utf-8") == original_text


# --- D2) rollback must also restore source-state.json chunk hashes -------


def test_rollback_restores_source_state_for_a_new_page_it_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fix, rollback restored/removed the page file but left the
    ``source-state.json`` ``applied_chunks`` entry behind. For a brand-new
    page that the rollback deletes, that meant a chunk hash stayed recorded
    for a page that -- as far as the vault is concerned -- was never
    written, permanently blocking ``_duplicate_source_chunk`` from ever
    letting that same source chunk apply again."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)
    service._active_pass = CompileEnrichPass(pass_id="pass-d2a", snapshot_enabled=True)
    monkeypatch.setattr(
        service.runner, "run", lambda *a, **k: json.dumps(_minimal_compile_payload())
    )

    result = service._upsert_briefing(
        target=_demo_target(domain="people", slug="brand-new-d2", title="Brand New D2"),
        source_rel_path="daily/2026-08-05.md",
        source_excerpt="A new fact for D2.",
        signal=None,
    )
    assert result.written is True

    page_text = (vault_path / result.path).read_text(encoding="utf-8")
    assert (
        service._duplicate_source_chunk(
            existing_text=page_text,
            source_rel_path="daily/2026-08-05.md",
            source_excerpt="A new fact for D2.",
            page_rel_path=result.path,
        )
        is True
    )
    assert result.path in service._load_source_state()["entries"]

    rollback = service.rollback_compile_enrich_pass("pass-d2a")

    assert rollback["restored"] == [result.path]
    assert not (vault_path / result.path).exists()
    assert result.path not in service._load_source_state()["entries"]


def test_rollback_restores_source_state_for_an_existing_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same defect as above, for a page that already existed before the
    pass: rollback must put the page's ``source-state.json`` entry back to
    its exact pre-pass shape (including an unrelated chunk hash recorded
    before this pass started), not just drop the pass's own addition."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)

    page_path = vault_path / "compiled" / "projects" / "demo-project.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    original_text = (
        "---\ndomain: projects\n---\n\n# Demo Project\n\n"
        "## Sources\n- [[daily/2026-08-01.md]]\n"
    )
    page_path.write_text(original_text, encoding="utf-8")
    rel_path = "compiled/projects/demo-project.md"
    service._record_source_state(
        rel_path,
        original_text,
        source_rel_path="daily/2026-08-01.md",
        source_excerpt="Old fact.",
    )
    pre_pass_entry = service._load_source_state()["entries"][rel_path]

    service._active_pass = CompileEnrichPass(pass_id="pass-d2b", snapshot_enabled=True)
    monkeypatch.setattr(
        service.runner, "run", lambda *a, **k: json.dumps(_minimal_compile_payload())
    )

    result = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-05.md",
        source_excerpt="New fact for D2b.",
        signal=None,
    )
    assert result.written is True
    assert result.path == rel_path

    mid_pass_entry = service._load_source_state()["entries"][rel_path]
    assert "daily/2026-08-05.md" in mid_pass_entry.get("applied_chunks", {})

    rollback = service.rollback_compile_enrich_pass("pass-d2b")

    assert rollback["restored"] == [rel_path]
    assert page_path.read_text(encoding="utf-8") == original_text
    restored_entry = service._load_source_state()["entries"][rel_path]
    assert restored_entry == pre_pass_entry
    assert "daily/2026-08-05.md" not in restored_entry.get("applied_chunks", {})


# --- E) the conflict retry must not report a false "inferred" trust ------


def test_settle_page_conflicts_recovers_daily_voice_trust_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_settle_page_conflicts`` (the nightly retry for conflicts left
    ``unclear`` the first time) used to call
    ``_source_trust_level(new_source, "")`` -- an empty excerpt makes a
    ``daily/`` source's trust rule fail closed to ``inferred`` every time,
    even though the source excerpt that produced the claim was the owner's
    own ``[voice]`` entry (trust ``own``). The fix recovers the real
    excerpt from disk via ``_source_excerpt`` before rating it."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)

    daily_path = vault_path / "daily" / "2026-08-05.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        "## 09:00 [voice]\nДедлайн — 15 сентября.\n", encoding="utf-8"
    )

    _conflict_page_on_disk(
        vault_path,
        shaped_rows=[
            ("2026-07-01", "thoughts/idea.md", "Дедлайн — 1 сентября."),
            ("2026-08-05", "daily/2026-08-05.md", "Дедлайн — 15 сентября."),
        ],
        conflict_rows=[
            (
                "2026-08-05",
                "Дедлайн — 1 сентября.",
                "thoughts/idea.md",
                "Дедлайн — 15 сентября.",
                "daily/2026-08-05.md",
            )
        ],
    )
    asked = _stub_adjudicator(monkeypatch, service, ("unclear", ""))

    service._resolve_open_conflicts(limit=5)

    assert len(asked) == 1
    assert asked[0]["new_trust"] == "own"


# --- C, continued) worker-busy with a live heartbeat is skipped, not failed


def test_run_nightly_maintenance_treats_live_worker_busy_as_skipped_not_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the nightly pass's own queue drain finds the worker lock already
    held by another, live worker, that is ordinary contention, not a
    failure: the pass journal (``.session/compile-enrich.json``) must record
    ``status: "no-work"``, not ``"failed"``, and the returned ``errors``
    list must stay empty rather than carry ``worker-busy`` as though
    something went wrong. Uses this test process's own pid for a genuinely
    live heartbeat, exercising the real ``_worker_state_is_live`` check
    instead of stubbing it out."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    service._write_worker_state(pid=os.getpid(), status="running", started_at=None)
    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {
            "drained": 0,
            "updated": [],
            "consolidations": [],
            "errors": ["worker-busy"],
        },
    )
    monkeypatch.setattr(service, "lint_notes", lambda: [])
    monkeypatch.setattr(service, "_archive_stale_notes", lambda limit=5: [])
    monkeypatch.setattr(service, "_refresh_qmd_index", lambda: None)

    result = service.run_nightly_maintenance()

    assert result["queue_busy"] is True
    assert result["queue_worker_pid"] == os.getpid()
    assert result["queue_errors"] == []
    assert result["errors"] == []

    journal = json.loads(
        (vault_path / ".session" / "compile-enrich.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["status"] == "no-work"
    assert journal["error"] == ""


# --- rollback must refresh the qmd search index when it restores anything -


def test_run_nightly_maintenance_refreshes_qmd_index_after_effective_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_nightly_maintenance`` already refreshes the qmd search index
    once, right after its own drain/backfill/etc. writes. When the inv-5
    effectiveness gate then rolls those same writes back, the index must be
    refreshed again -- otherwise it keeps pointing at content the rollback
    just reverted. Isolates the wiring by stubbing
    ``rollback_compile_enrich_pass`` itself (its own restore behavior is
    covered elsewhere) and spying on ``_refresh_qmd_index``."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {
            "drained": 1,
            "updated": [],
            "consolidations": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(service, "lint_notes", lambda: [])
    monkeypatch.setattr(service, "_archive_stale_notes", lambda limit=5: [])
    monkeypatch.setattr(
        service,
        "rollback_compile_enrich_pass",
        lambda pass_id: {
            "restored": ["compiled/projects/demo-project.md"],
            "skipped": [],
            "manifest_found": True,
        },
    )
    refresh_calls = {"count": 0}
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refresh_calls.__setitem__("count", refresh_calls["count"] + 1),
    )

    service.run_nightly_maintenance()

    assert refresh_calls["count"] == 1


def test_run_nightly_maintenance_does_not_refresh_qmd_index_after_empty_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same gate as above, but the rollback restores nothing (a pass that
    took work yet never actually wrote a page has nothing to roll back) --
    an extra index refresh here would be pure waste."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {
            "drained": 1,
            "updated": [],
            "consolidations": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(service, "lint_notes", lambda: [])
    monkeypatch.setattr(service, "_archive_stale_notes", lambda limit=5: [])
    monkeypatch.setattr(
        service,
        "rollback_compile_enrich_pass",
        lambda pass_id: {"restored": [], "skipped": [], "manifest_found": True},
    )
    refresh_calls = {"count": 0}
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refresh_calls.__setitem__("count", refresh_calls["count"] + 1),
    )

    service.run_nightly_maintenance()

    assert refresh_calls["count"] == 0


def test_cli_run_rollback_refreshes_qmd_index_only_when_something_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--rollback`` CLI branch (``run_compiled_pass.py``'s
    ``_run_rollback``) has no later pass step to refresh the qmd index for
    it, unlike ``run_nightly_maintenance`` -- it must do its own refresh,
    and only when the rollback actually restored something."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    refresh_calls = {"count": 0}
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refresh_calls.__setitem__("count", refresh_calls["count"] + 1),
    )

    monkeypatch.setattr(
        service,
        "rollback_compile_enrich_pass",
        lambda pass_id: {
            "restored": ["compiled/projects/demo-project.md"],
            "skipped": [],
            "manifest_found": True,
        },
    )
    _run_rollback(service, "pass-with-restore")
    assert refresh_calls["count"] == 1

    monkeypatch.setattr(
        service,
        "rollback_compile_enrich_pass",
        lambda pass_id: {"restored": [], "skipped": [], "manifest_found": True},
    )
    _run_rollback(service, "pass-without-restore")
    assert refresh_calls["count"] == 1


# --- D1+D2 combined: two upserts to one page, then rollback ---------------


def test_rollback_after_two_upserts_to_same_page_restores_file_and_source_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression covering D1 and D2 together on the same page: two upserts
    to the SAME page inside one pass, followed by rollback, must put both
    the page file and its ``source-state.json`` entry back to how they were
    BEFORE the FIRST upsert -- not to some intermediate state reflecting
    only the second write, and not leaving either upsert's applied-chunk
    hash stuck in ``source-state.json`` where ``_duplicate_source_chunk``
    would forever block re-applying it."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _bypass_atomic_vault_write(monkeypatch)

    page_path = vault_path / "compiled" / "projects" / "demo-project.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    original_text = (
        "---\ndomain: projects\n---\n\n# Demo Project\n\n"
        "## Sources\n- [[daily/2026-08-01.md]]\n"
    )
    page_path.write_text(original_text, encoding="utf-8")
    rel_path = "compiled/projects/demo-project.md"
    service._record_source_state(
        rel_path,
        original_text,
        source_rel_path="daily/2026-08-01.md",
        source_excerpt="Old fact.",
    )
    pre_pass_entry = service._load_source_state()["entries"][rel_path]

    service._active_pass = CompileEnrichPass(
        pass_id="pass-combo", snapshot_enabled=True
    )
    monkeypatch.setattr(
        service.runner, "run", lambda *a, **k: json.dumps(_minimal_compile_payload())
    )

    first = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-05.md",
        source_excerpt="First update.",
        signal=None,
    )
    second = service._upsert_briefing(
        target=_demo_target(),
        source_rel_path="daily/2026-08-06.md",
        source_excerpt="Second update.",
        signal=None,
    )
    assert first.written is True
    assert second.written is True
    assert first.path == rel_path
    assert second.path == rel_path

    rollback = service.rollback_compile_enrich_pass("pass-combo")

    assert rollback["restored"] == [rel_path]
    assert page_path.read_text(encoding="utf-8") == original_text
    restored_entry = service._load_source_state()["entries"][rel_path]
    assert restored_entry == pre_pass_entry
    applied_chunks = restored_entry.get("applied_chunks", {})
    assert "daily/2026-08-05.md" not in applied_chunks
    assert "daily/2026-08-06.md" not in applied_chunks
