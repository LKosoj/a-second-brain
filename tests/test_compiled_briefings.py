import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from conftest import _write_vault_manifest

from d_brain.services.compiled_briefings import (
    IMPACT_CATALOG_MAX_CHARS,
    IMPACT_TIMEOUT_SECONDS,
    CompiledBatchConsolidationEvent,
    CompiledBriefingService,
    CompiledBriefingTarget,
)
from d_brain.services.frontmatter import write_validated_vault_markdown


def _compiled_service(vault_path: Path) -> CompiledBriefingService:
    vault_path.mkdir(parents=True, exist_ok=True)
    _write_vault_manifest(vault_path)
    return CompiledBriefingService(vault_path)


def test_compiled_briefings_question_context_prefers_matching_note(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled"
    (compiled_root / "projects").mkdir(parents=True)
    (compiled_root / "people").mkdir(parents=True)

    (compiled_root / "projects" / "example-project.md").write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Операционный briefing по Example Project."\n'
            "freshness_state: fresh\n"
            "confidence: high\n"
            "relevance: 0.93\n"
            "tier: active\n"
            "---\n\n"
            "# Example Project\n\n"
            "## Current State\n"
            "Есть активный трек по проекту.\n\n"
            "## Sources\n"
            "- [[daily/2026-04-04.md]]\n"
        ),
        encoding="utf-8",
    )
    (compiled_root / "people" / "ivan-petrov.md").write_text(
        (
            "---\n"
            "domain: people\n"
            'description: "Контакт по другой теме."\n'
            "freshness_state: watch\n"
            "confidence: medium\n"
            "relevance: 0.60\n"
            "tier: warm\n"
            "---\n\n"
            "# Иван Петров\n\n"
            "## Current State\n"
            "Отдельный контекст.\n"
        ),
        encoding="utf-8",
    )

    block = _compiled_service(vault_path).build_question_context(
        "Что сейчас по Example Project?"
    )

    assert "compiled/projects/example-project.md" in block
    assert "Операционный briefing по Example Project." in block
    assert "compiled/people/ivan-petrov.md" not in block


def test_compiled_briefings_lint_resolves_project_root_relative_sources(
    tmp_path: Path,
) -> None:
    today = date.today().isoformat()
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    compiled_root.mkdir(parents=True)
    (vault_path / "daily").mkdir(parents=True)
    (tmp_path / "skills" / "vault-health" / "scripts").mkdir(parents=True)
    (vault_path / "skills" / "private" / "local-skill").mkdir(parents=True)
    (tmp_path / "skills" / "private").symlink_to(
        "../vault/skills/private",
        target_is_directory=True,
    )
    (vault_path / ".compiled").mkdir(parents=True)
    (vault_path / ".sync").mkdir(parents=True)
    (vault_path / "MOC").mkdir(parents=True)
    (tmp_path / "src" / "d_brain" / "services").mkdir(parents=True)
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "deploy").mkdir(parents=True)

    (vault_path / "daily" / "2026-04-11.md").write_text("# Daily\n", encoding="utf-8")
    (vault_path / "daily" / "2026-04-10.md").write_text("# Daily\n", encoding="utf-8")
    (tmp_path / "skills" / "vault-health" / "scripts" / "backlinks.sh").write_text(
        "#!/usr/bin/env bash\n",
        encoding="utf-8",
    )
    (vault_path / "skills/private/local-skill/SKILL.md").write_text(
        "# Local skill\n",
        encoding="utf-8",
    )
    (vault_path / ".compiled" / "compiled-briefing-system.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (vault_path / ".sync" / "plaud-state.json").write_text("{}\n", encoding="utf-8")
    (vault_path / "MOC" / "index.md").write_text("# MOC\n", encoding="utf-8")
    (tmp_path / "src" / "d_brain" / "services" / "compiled_briefings.py").write_text(
        "# stub\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "setup_control_plane.sh").write_text(
        "#!/usr/bin/env bash\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "control-plane.md").write_text("# Docs\n", encoding="utf-8")
    (tmp_path / "deploy" / "a-second-brain-plaud-sync.service").write_text(
        "[Service]\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    (tmp_path / "README.ru.md").write_text("# Проект\n", encoding="utf-8")

    (compiled_root / "resolver-demo.md").write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Resolver coverage."\n'
            "freshness_state: fresh\n"
            "confidence: high\n"
            f"updated: {today}\n"
            "relevance: 0.90\n"
            "tier: active\n"
            "---\n\n"
            "# Resolver Demo\n\n"
            "## Current State\n"
            "Проверка source resolver.\n\n"
            "## Recent Changes\n"
            "- Added support check.\n\n"
            "## Open Loops\n"
            "- Validate missing path.\n\n"
            "## Key Decisions\n"
            "- Keep resolver deterministic.\n\n"
            "## Next Check\n"
            "Smoke test.\n\n"
            "## Sources\n"
            "- [[daily/2026-04-11.md]]\n"
            "- [[vault/daily/2026-04-10.md]]\n"
            "- [[skills/vault-health/scripts/backlinks.sh]]\n"
            "- [[vault/skills/private/local-skill/SKILL.md]]\n"
            "- [[compiled/compiled-briefing-system.json]]\n"
            "- [[sync/plaud-state.json]]\n"
            "- [[MOC/index]]\n"
            "- [[README.md]]\n"
            "- [[README.ru.md]]\n"
            "- [[deploy/a-second-brain-plaud-sync.service]]\n"
            "- [[src/d_brain/services/compiled_briefings.py]]\n"
            "- [[scripts/setup_control_plane.sh]]\n"
            "- [[docs/control-plane.md]]\n"
            "- [[src/missing.py]]\n"
        ),
        encoding="utf-8",
    )

    issues = _compiled_service(vault_path).lint_notes()

    assert issues == [
        {
            "path": "compiled/projects/resolver-demo.md",
            "issue": "broken-source-link",
            "detail": "src/missing.py",
        }
    ]


def test_compiled_briefings_nightly_lints_after_archive_and_separates_freshness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    daily_root = vault_path / "daily"
    compiled_root.mkdir(parents=True)
    daily_root.mkdir(parents=True)
    (daily_root / "2026-04-11.md").write_text("# Daily\n", encoding="utf-8")
    note_path = compiled_root / "demo.md"
    note_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Needs refresh."\n'
            "freshness_state: stale\n"
            "confidence: high\n"
            "updated: 2026-04-01\n"
            "relevance: 0.90\n"
            "tier: active\n"
            "---\n\n"
            "# Demo\n\n"
            "## Current State\n"
            "Old state.\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    refreshed = {"count": 0}

    def fake_archive(*, limit: int) -> list[str]:
        assert limit == 5
        note_path.unlink()
        return ["compiled/archive/projects/demo.md"]

    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {  # noqa: ANN001
            "drained": 0,
            "updated": [],
            "consolidations": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(service, "_archive_stale_notes", fake_archive)
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refreshed.__setitem__("count", refreshed["count"] + 1),
    )

    result = service.run_nightly_maintenance()

    assert result["archived"] == ["compiled/archive/projects/demo.md"]
    assert result["backfilled"] == []
    assert result["lint_issues"] == []
    assert result["freshness_issues"] == []
    assert refreshed["count"] == 1


def test_compiled_briefings_source_snapshot_ignores_age_and_detects_change(
    tmp_path: Path,
) -> None:
    old_day = (date.today() - timedelta(days=8)).isoformat()
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    daily_root = vault_path / "daily"
    compiled_root.mkdir(parents=True)
    daily_root.mkdir(parents=True)
    source_path = daily_root / "2026-04-11.md"
    source_path.write_text("# Daily\n\nStable fact.\n", encoding="utf-8")
    note_path = compiled_root / "demo.md"
    note_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Aging."\n'
            "freshness_state: fresh\n"
            "confidence: high\n"
            f"last_compiled_at: {old_day}\n"
            "relevance: 0.90\n"
            "tier: active\n"
            "---\n\n"
            "# Demo\n\n"
            "## Sources\n"
            "- [[daily/2026-04-11.md]]\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    original_note = note_path.read_text(encoding="utf-8")

    initialized = service.initialize_source_state()

    assert initialized["evaluated"] == 1
    assert initialized["initialized"] == 1
    assert service.freshness_issues() == []
    assert note_path.read_text(encoding="utf-8") == original_note

    source_path.write_text("# Daily\n\nChanged fact.\n", encoding="utf-8")

    repeated = service.initialize_source_state()

    assert repeated["initialized"] == 0
    assert repeated["changed"] == 1
    assert service.freshness_issues() == [
        {
            "path": "compiled/projects/demo.md",
            "issue": "source-changed",
            "detail": "changed_sources=daily/2026-04-11.md",
        }
    ]


def test_compiled_briefings_source_snapshot_ignores_memory_touch(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    source_path = vault_path / "thoughts" / "demo.md"
    handoff_path = vault_path / ".session" / "handoff.md"
    compiled_root.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text("Transient state.\n", encoding="utf-8")
    source_path.write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-07-01\n"
            "relevance: 0.81\n"
            "tier: warm\n"
            "---\n\n"
            "# Stable source\n"
        ),
        encoding="utf-8",
    )
    (compiled_root / "demo.md").write_text(
        (
            "---\n"
            "domain: projects\n"
            "freshness_state: fresh\n"
            "---\n\n"
            "# Demo\n\n"
            "## Sources\n"
            "- [[thoughts/demo.md]]\n"
            "- [[.session/handoff.md]]\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    service.initialize_source_state()

    source_path.write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-07-23\n"
            "relevance: 0.94\n"
            "tier: active\n"
            "---\n\n"
            "# Stable source\n"
        ),
        encoding="utf-8",
    )
    handoff_path.write_text("Changed transient state.\n", encoding="utf-8")

    assert service.freshness_issues() == []


def test_compiled_briefings_backfill_targets_changed_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    old_day = (date.today() - timedelta(days=8)).isoformat()
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    daily_root = vault_path / "daily"
    compiled_root.mkdir(parents=True)
    daily_root.mkdir(parents=True)
    for index in range(2):
        day = f"2026-04-1{index}.md"
        (daily_root / day).write_text("# Daily\n", encoding="utf-8")
        (compiled_root / f"demo-{index}.md").write_text(
            (
                "---\n"
                "domain: projects\n"
                'description: "Aging."\n'
                "freshness_state: fresh\n"
                f"last_compiled_at: {old_day}\n"
                "---\n\n"
                f"# Demo {index}\n\n"
                "## Sources\n"
                f"- [[daily/{day}]]\n"
            ),
            encoding="utf-8",
        )
    service = _compiled_service(vault_path)
    service.initialize_source_state()
    (daily_root / "2026-04-10.md").write_text("# Changed\n", encoding="utf-8")
    targets: list[CompiledBriefingTarget] = []

    def fake_upsert_briefing(**kwargs):  # noqa: ANN001, ANN202
        targets.append(kwargs["target"])
        return str(kwargs["target"].existing_path)

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service, "_upsert_briefing", fake_upsert_briefing)

    result = service._backfill_freshness_notes(limit=1)

    assert result == ["compiled/projects/demo-0.md"]
    assert [target.existing_path for target in targets] == [
        "compiled/projects/demo-0.md"
    ]


def test_compiled_briefings_backfill_processes_every_changed_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    daily_root = vault_path / "daily"
    compiled_root.mkdir(parents=True)
    daily_root.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (daily_root / name).write_text("Before.\n", encoding="utf-8")
    note_path = compiled_root / "demo.md"
    note_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            "freshness_state: fresh\n"
            "---\n\n"
            "# Demo\n\n"
            "## Sources\n"
            "- [[daily/a.md]]\n"
            "- [[daily/b.md]]\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    service.initialize_source_state()
    for name in ("a.md", "b.md"):
        (daily_root / name).write_text("After.\n", encoding="utf-8")
    refreshed_sources: list[str] = []

    def fake_upsert_briefing(**kwargs):  # noqa: ANN001, ANN202
        refreshed_sources.append(str(kwargs["source_rel_path"]))
        return "compiled/projects/demo.md"

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service, "_upsert_briefing", fake_upsert_briefing)

    assert service._backfill_freshness_notes(limit=1) == ["compiled/projects/demo.md"]
    assert refreshed_sources == ["daily/a.md", "daily/b.md"]
    assert service.freshness_issues() == []


def test_compiled_briefings_enqueue_refresh_deduplicates_source_path(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    first = service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="first",
    )
    second = service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="second",
    )

    queue = json.loads(
        (vault_path / ".compiled" / "queue.json").read_text(encoding="utf-8")
    )

    assert first["queued"] is True
    assert second["queued"] is True
    assert len(queue) == 1
    assert queue[0]["source_path"] == "daily/2026-04-04.md"
    assert queue[0]["source_excerpt"] == "second"


def test_compiled_briefings_target_path_rejects_traversal(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    target = CompiledBriefingTarget(
        domain="projects",
        title="Demo",
        slug="demo",
        description="",
        reason="",
        existing_path="compiled/../../README.md",
    )

    assert service._target_path(target) == (
        vault_path.resolve() / "compiled/projects/demo.md"
    )


def test_compiled_briefings_drain_queue_reports_non_retriable_errors(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    service.refresh_after_write = lambda **kwargs: {  # type: ignore[method-assign]
        "updated": [],
        "errors": ["unsupported-path"],
    }
    service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="body",
        debounce_seconds=0,
    )

    result = service.drain_queue(force=True, refresh_qmd=False)

    assert result["errors"] == ["unsupported-path"]


def test_compiled_briefings_archives_stale_notes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    note_path = vault_path / "compiled" / "projects" / "old.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Old."\n'
            "status: done\n"
            "freshness_state: stale\n"
            "confidence: high\n"
            "updated: 2026-01-01\n"
            "relevance: 0.90\n"
            "tier: cold\n"
            "---\n\n"
            "# Old\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    monkeypatch.setattr(service, "is_available", lambda: True)

    archived = service._archive_stale_notes(limit=5)

    assert archived == ["compiled/archive/projects/old.md"]
    assert not note_path.exists()
    assert (vault_path / "compiled" / "archive" / "projects" / "old.md").exists()
    assert service._iter_candidates() == []


def test_compiled_briefings_archive_skips_aging_without_stale_flag(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    compiled_root.mkdir(parents=True)
    old_day = (date.today() - timedelta(days=8)).isoformat()
    stale_path = compiled_root / "stale.md"
    aging_path = compiled_root / "aging.md"
    stale_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Explicit stale."\n'
            "status: done\n"
            "freshness_state: stale\n"
            f"last_compiled_at: {old_day}\n"
            "---\n\n"
            "# Stale\n"
        ),
        encoding="utf-8",
    )
    aging_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Only aging."\n'
            "freshness_state: fresh\n"
            f"last_compiled_at: {old_day}\n"
            "---\n\n"
            "# Aging\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)

    archived = service._archive_stale_notes(limit=5)

    assert archived == ["compiled/archive/projects/stale.md"]
    assert not stale_path.exists()
    assert aging_path.exists()


def test_compiled_briefings_archive_keeps_active_stale_note(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    note_path = vault_path / "compiled" / "projects" / "active.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            "status: active\n"
            "freshness_state: stale\n"
            "---\n\n"
            "# Active\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)

    assert service._archive_stale_notes(limit=5) == []
    assert note_path.exists()


def test_compiled_briefings_spawn_background_drain_is_single_flight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    spawned: list[list[str]] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        spawned.append(command)
        return FakeProcess()

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service, "_pid_is_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    first = service.spawn_background_drain()
    second = service.spawn_background_drain()

    state = json.loads(
        (vault_path / ".compiled" / "worker-state.json").read_text(encoding="utf-8")
    )

    assert first is True
    assert second is False
    assert spawned == [
        [
            sys.executable,
            "-m",
            "d_brain.run_compiled_maintenance",
            "--queue-only",
        ]
    ]
    assert state["pid"] == 4321
    assert state["status"] == "starting"


def test_compiled_briefings_run_nightly_marks_live_worker_busy_without_queue_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {  # noqa: ANN001, ANN202
            "drained": 0,
            "updated": [],
            "consolidations": [],
            "errors": ["worker-busy"],
        },
    )
    monkeypatch.setattr(service, "_load_worker_state", lambda: {"pid": 4321})
    monkeypatch.setattr(service, "_worker_state_is_live", lambda state: True)
    monkeypatch.setattr(service, "lint_notes", lambda: [])
    monkeypatch.setattr(service, "_archive_stale_notes", lambda limit=5: [])
    monkeypatch.setattr(service, "_refresh_qmd_index", lambda: None)

    result = service.run_nightly_maintenance()

    assert result["queue_busy"] is True
    assert result["queue_worker_pid"] == 4321
    assert result["queue_errors"] == []
    assert result["errors"] == []


def test_compiled_briefings_run_queue_worker_drains_burst_until_queue_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    refreshed = {"count": 0}
    seen: list[str] = []

    service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="first",
        debounce_seconds=0,
    )
    service.enqueue_refresh(
        source_path="daily/2026-04-05.md",
        source_excerpt="second",
        debounce_seconds=0,
    )

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(
        service,
        "refresh_after_write",
        lambda *, source_path, source_excerpt="", max_updates=3: (  # type: ignore[no-untyped-def]
            seen.append(source_path)
            or {
                "updated": [f"compiled/{Path(source_path).stem}.md"],
                "errors": [],
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refreshed.__setitem__("count", refreshed["count"] + 1),
    )

    result = service.run_queue_worker(
        force=True,
        max_events=1,
        refresh_qmd=True,
        idle_seconds=0,
        poll_seconds=0.0,
    )

    assert result["drained"] == 2
    assert seen == ["daily/2026-04-04.md", "daily/2026-04-05.md"]
    assert result["updated"] == [
        "compiled/2026-04-04.md",
        "compiled/2026-04-05.md",
    ]
    assert refreshed["count"] == 1
    assert not (vault_path / ".compiled" / "worker-state.json").exists()
    assert (
        json.loads(
            (vault_path / ".compiled" / "queue.json").read_text(encoding="utf-8")
        )
        == []
    )


def test_compiled_briefings_normalize_paths_preserves_skills_and_project_root(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    absolute_project_path = (
        tmp_path / "src" / "d_brain" / "services" / "processor.py"
    ).resolve()

    normalized = service._normalize_paths(
        [
            "skills/vault-health/SKILL.md",
            "vault/.claude/docs/prompt-source-map.md",
            str(absolute_project_path),
        ]
    )

    assert normalized == [
        "skills/vault-health/SKILL.md",
        ".claude/docs/prompt-source-map.md",
        "src/d_brain/services/processor.py",
    ]


def test_compiled_briefings_run_queue_worker_writes_batch_consolidation_note(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    refreshed = {"count": 0}

    service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="Alice approved the budget and highlighted delivery risk.",
        debounce_seconds=0,
    )
    service.enqueue_refresh(
        source_path="imports/plaud/notes/2026/04/demo.md",
        source_excerpt="Follow-up meeting repeated the same budget and delivery risk.",
        debounce_seconds=0,
    )

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(
        service,
        "refresh_after_write",
        lambda *, source_path, source_excerpt="", max_updates=3: {  # type: ignore[no-untyped-def]
            "updated": [f"compiled/{Path(source_path).stem}.md"],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        service,
        "_refresh_qmd_index",
        lambda: refreshed.__setitem__("count", refreshed["count"] + 1),
    )
    monkeypatch.setattr(
        service.runner,
        "run",
        lambda prompt, *, timeout: json.dumps(  # type: ignore[no-untyped-def]
            {
                "headline": "Бюджет и delivery risk повторяются",
                "summary": (
                    "Два независимых источника подтверждают один и тот же "
                    "operational risk."
                ),
                "themes": [
                    "Бюджетный контур связан с риском поставки.",
                    "Тема повторяется в нескольких рабочих событиях.",
                ],
                "follow_ups": [
                    "Проверить следующий статус по риску.",
                ],
            },
            ensure_ascii=False,
        ),
    )

    result = service.run_queue_worker(
        force=True,
        max_events=2,
        refresh_qmd=True,
        idle_seconds=0,
        poll_seconds=0.0,
    )

    assert result["drained"] == 2
    assert len(result["consolidations"]) == 1
    note_path = vault_path / result["consolidations"][0]
    assert note_path.exists()
    note = note_path.read_text(encoding="utf-8")
    assert "# Бюджет и delivery risk повторяются" in note
    assert "## Updated Briefings" in note
    assert "[[daily/2026-04-04.md]]" in note
    assert "[[imports/plaud/notes/2026/04/demo.md]]" in note
    assert refreshed["count"] == 1


def test_compiled_briefings_batch_consolidation_requires_multiple_sources(
    tmp_path: Path,
) -> None:
    service = _compiled_service(tmp_path / "vault")

    result = service._write_batch_consolidation(
        [
            CompiledBatchConsolidationEvent(
                source_rel_path="daily/2026-04-04.md",
                source_excerpt="Single source excerpt.",
                updated_paths=("compiled/projects/demo.md",),
            )
        ]
    )

    assert result is None


def test_compiled_briefings_ack_after_success_keeps_reenqueued_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    seen: list[str] = []

    service.enqueue_refresh(
        source_path="daily/2026-04-04.md",
        source_excerpt="first",
        debounce_seconds=0,
    )

    def fake_refresh_after_write(  # type: ignore[no-untyped-def]
        *,
        source_path,
        source_excerpt="",
        max_updates=3,
    ):
        del source_path, max_updates
        seen.append(source_excerpt)
        if len(seen) == 1:
            service.enqueue_refresh(
                source_path="daily/2026-04-04.md",
                source_excerpt="second",
            )
        return {"updated": ["compiled/demo.md"], "errors": []}

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service, "refresh_after_write", fake_refresh_after_write)

    result = service.run_queue_worker(
        force=False,
        max_events=1,
        refresh_qmd=False,
        idle_seconds=0,
        poll_seconds=0.0,
    )
    queue = json.loads(
        (vault_path / ".compiled" / "queue.json").read_text(encoding="utf-8")
    )

    assert result["drained"] == 1
    assert seen == ["first"]
    assert len(queue) == 1
    assert queue[0]["source_path"] == "daily/2026-04-04.md"
    assert queue[0]["source_excerpt"] == "second"
    assert queue[0]["state"] == "pending"
    assert queue[0]["claim_token"] == ""


def test_compiled_briefings_drain_queue_recovers_stale_inflight_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    (vault_path / ".compiled").mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    queue_path = vault_path / ".compiled" / "queue.json"
    queue_path.write_text(
        json.dumps(
            [
                {
                    "source_path": "daily/2026-04-04.md",
                    "source_excerpt": "stale",
                    "enqueued_at": now.isoformat(),
                    "last_enqueued_at": now.isoformat(),
                    "due_at": now.timestamp(),
                    "attempts": 0,
                    "max_updates": 3,
                    "state": "in_flight",
                    "claim_token": "dead-token",
                    "claimed_at": (now - timedelta(hours=1)).isoformat(),
                    "claimed_pid": 999999,
                }
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(
        service,
        "refresh_after_write",
        lambda *, source_path, source_excerpt="", max_updates=3: {  # type: ignore[no-untyped-def]
            "updated": [f"compiled/{Path(source_path).stem}.md"],
            "errors": [],
        },
    )

    result = service.drain_queue(force=False, max_events=1, refresh_qmd=False)

    assert result["drained"] == 1
    assert result["updated"] == ["compiled/2026-04-04.md"]
    assert json.loads(queue_path.read_text(encoding="utf-8")) == []


def test_compiled_briefings_file_output_artifact_persists_note_and_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    spawned: list[bool] = []

    monkeypatch.setattr(
        CompiledBriefingService,
        "spawn_background_drain",
        lambda self: spawned.append(True) or True,
    )

    rel_path = service.file_output_artifact(
        request="Что сейчас по Example Project?",
        output_markdown=(
            "Статус по теме.\n\n"
            "- факт один с деталями и контекстом\n"
            "- факт два с деталями и контекстом\n"
            "- факт три с деталями и контекстом\n"
            "- факт четыре с деталями и контекстом\n"
            "- факт пять с деталями и контекстом\n"
            "- факт шесть с деталями и контекстом\n"
        ),
        artifact_type="question-answer",
    )

    assert rel_path is not None
    artifact_path = vault_path / rel_path
    assert artifact_path.exists()
    artifact = artifact_path.read_text(encoding="utf-8")
    assert "## Request" in artifact
    assert "type: assistant-output" in artifact
    assert "description:" in artifact
    assert "last_accessed:" in artifact
    assert "tier: active" in artifact
    queue = json.loads(
        (vault_path / ".compiled" / "queue.json").read_text(encoding="utf-8")
    )
    assert queue[0]["source_path"] == rel_path
    assert spawned == [True]


def test_compiled_briefings_impact_prompt_handles_mixed_daily_notes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)

    prompt = service._build_impact_prompt(
        source_rel_path="daily/2025-04-29.md",
        source_excerpt=(
            "Один день с несколькими темами: incident, partner strategy, testing."
        ),
        signal={"tier": "active", "relevance": 0.99},
        catalog=[],
        max_updates=3,
    )

    assert "multiple unrelated durable threads" in prompt
    assert "daily notes that bundle several meetings, incidents, decisions" in prompt
    assert '"source_shape": "single|mixed|noisy"' in prompt
    assert '"durable_threads": [' in prompt
    assert '{ "source_shape": "noisy", "durable_threads": [], "updates": [] }' in prompt
    assert "Mixed daily-note example" in prompt


def test_compiled_briefings_impact_catalog_is_bounded_and_source_aware(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    compiled_root = vault_path / "compiled" / "projects"
    compiled_root.mkdir(parents=True)

    for index in range(80):
        title = f"Generic Project {index}"
        description = ("generic background " * 20).strip()
        (compiled_root / f"generic-{index}.md").write_text(
            (
                "---\n"
                "domain: projects\n"
                f'description: "{description}"\n'
                "freshness_state: watch\n"
                "confidence: medium\n"
                "relevance: 0.40\n"
                "tier: warm\n"
                "---\n\n"
                f"# {title}\n"
            ),
            encoding="utf-8",
        )

    matching_path = compiled_root / "example-project-risk.md"
    matching_path.write_text(
        (
            "---\n"
            "domain: projects\n"
            'description: "Example Project migration risk and recovery track."\n'
            "freshness_state: fresh\n"
            "confidence: high\n"
            "relevance: 0.95\n"
            "tier: active\n"
            "---\n\n"
            "# Example Project Risk\n\n"
            "## Sources\n"
            "- [[daily/2025-11-10.md]]\n"
        ),
        encoding="utf-8",
    )

    service = _compiled_service(vault_path)
    catalog = service._impact_catalog(
        source_rel_path="daily/2025-11-10.md",
        source_excerpt=(
            "Example Project migration risk escalated after infrastructure incident."
        ),
    )

    serialized = json.dumps(catalog, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= IMPACT_CATALOG_MAX_CHARS
    assert any(item["slug"] == "example-project-risk" for item in catalog)
    assert len(catalog) < 81


def test_compiled_briefings_refresh_daily_fully_processes_all_entry_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    daily_path = vault_path / "daily" / "2025-04-30.md"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text(
        (
            "# 2025-04-30\n\n"
            "## 09:00 [text]\n"
            "Первый трек про проект А.\n\n"
            "## 12:00 [text]\n"
            "Второй трек про проект Б.\n\n"
            "## 18:00 [text]\n"
            "Третий трек про проект В.\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    excerpts: list[str] = []

    def fake_refresh(self, *, source_path, source_excerpt="", max_updates=3):  # noqa: ANN001
        del self, source_path, max_updates
        excerpts.append(source_excerpt)
        return {"available": True, "updated": [], "errors": []}

    monkeypatch.setattr(CompiledBriefingService, "refresh_after_write", fake_refresh)

    result = service.refresh_daily_fully(source_path="daily/2025-04-30.md")

    assert result["chunks"] == 3
    assert result["processed_chunks"] == 3
    assert any("Первый трек про проект А." in excerpt for excerpt in excerpts)
    assert any("Второй трек про проект Б." in excerpt for excerpt in excerpts)
    assert any("Третий трек про проект В." in excerpt for excerpt in excerpts)


def test_compiled_briefings_refresh_daily_fully_keeps_middle_of_large_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    daily_path = vault_path / "daily" / "2025-05-01.md"
    daily_path.parent.mkdir(parents=True)
    middle = "MIDDLE-MARKER-" + ("x" * 8200)
    daily_path.write_text(
        (f"# 2025-05-01\n\n## 10:00 [text]\nSTART-MARKER\n\n{middle}\n\nEND-MARKER\n"),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    excerpts: list[str] = []

    def fake_refresh(self, *, source_path, source_excerpt="", max_updates=3):  # noqa: ANN001
        del self, source_path, max_updates
        excerpts.append(source_excerpt)
        return {"available": True, "updated": [], "errors": []}

    monkeypatch.setattr(CompiledBriefingService, "refresh_after_write", fake_refresh)

    result = service.refresh_daily_fully(source_path="daily/2025-05-01.md")

    assert result["chunks"] >= 2
    combined = "\n".join(excerpts)
    assert "START-MARKER" in combined
    assert "MIDDLE-MARKER-" in combined
    assert "END-MARKER" in combined
    assert all("...[truncated]" not in excerpt for excerpt in excerpts)


def test_compiled_briefings_refresh_daily_fully_reports_chunk_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    daily_path = vault_path / "daily" / "2025-05-02.md"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text(
        (
            "# 2025-05-02\n\n"
            "## 09:00 [text]\n"
            "Первый блок.\n\n"
            "## 10:00 [text]\n"
            "Второй блок.\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    events: list[dict[str, object]] = []

    def fake_refresh(  # noqa: ANN001
        self,
        *,
        source_path,
        source_excerpt="",
        max_updates=3,
    ):
        del self, source_path, source_excerpt, max_updates
        return {
            "available": True,
            "updated": ["compiled/projects/demo.md"],
            "errors": [],
        }

    monkeypatch.setattr(CompiledBriefingService, "refresh_after_write", fake_refresh)

    result = service.refresh_daily_fully(
        source_path="daily/2025-05-02.md",
        on_chunk=events.append,
    )

    assert result["chunks"] == 2
    assert events == [
        {
            "index": 1,
            "total": 2,
            "status": "started",
            "source_rel_path": "daily/2025-05-02.md",
        },
        {
            "index": 1,
            "total": 2,
            "status": "finished",
            "source_rel_path": "daily/2025-05-02.md",
            "updated": ["compiled/projects/demo.md"],
            "errors": [],
        },
        {
            "index": 2,
            "total": 2,
            "status": "started",
            "source_rel_path": "daily/2025-05-02.md",
        },
        {
            "index": 2,
            "total": 2,
            "status": "finished",
            "source_rel_path": "daily/2025-05-02.md",
            "updated": ["compiled/projects/demo.md"],
            "errors": [],
        },
    ]


def test_compiled_briefings_resolve_targets_repairs_non_json_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir(parents=True)
    service = _compiled_service(vault_path)
    prompts: list[str] = []
    responses = iter(
        [
            "Сначала вот краткое объяснение без JSON.",
            json.dumps(
                {
                    "source_shape": "mixed",
                    "durable_threads": [
                        {"label": "project", "why": "recurring project thread"}
                    ],
                    "updates": [
                        {
                            "domain": "projects",
                            "title": "Demo Project",
                            "slug": "demo-project",
                            "description": "Demo snippet",
                            "reason": "durable thread",
                            "existing_path": "",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )

    def fake_run(prompt: str, *, timeout: int) -> str:
        del timeout
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(service.runner, "run", fake_run)

    targets = service._resolve_targets(
        source_rel_path="daily/2025-04-30.md",
        source_excerpt="# 2025-04-30\n\n## 09:00 [text]\nDemo update.",
        signal=None,
        max_updates=3,
    )

    assert [(target.domain, target.slug) for target in targets] == [
        ("projects", "demo-project")
    ]
    assert len(prompts) == 2
    assert "repairing model output" in prompts[1].lower()


def test_compiled_briefings_upsert_briefing_repairs_non_json_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir(parents=True)
    service = _compiled_service(vault_path)
    target = CompiledBriefingTarget(
        domain="projects",
        title="Demo Project",
        slug="demo-project",
        description="Demo snippet",
        reason="reason",
    )
    prompts: list[str] = []
    responses = iter(
        [
            "Ниже краткое описание, но не JSON.",
            json.dumps(
                {
                    "description": "Demo snippet",
                    "status": "active",
                    "freshness_state": "fresh",
                    "confidence": "medium",
                    "current_state": "Есть актуальное состояние.",
                    "recent_changes": ["Обновлён briefing."],
                    "open_loops": ["Проверить следующий шаг."],
                    "key_decisions": ["Оставить compiled note."],
                    "next_check": "После следующего апдейта.",
                    "source_links": ["daily/2025-04-30.md"],
                },
                ensure_ascii=False,
            ),
        ]
    )

    def fake_run(prompt: str, *, timeout: int) -> str:
        del timeout
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(service.runner, "run", fake_run)

    rel_path = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2025-04-30.md",
        source_excerpt="# 2025-04-30\n\n## 09:00 [text]\nDemo update.",
        signal=None,
    )

    note_path = vault_path / rel_path
    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert "# Demo Project" in content
    assert "Есть актуальное состояние." in content
    assert len(prompts) == 2
    assert "repairing model output" in prompts[1].lower()


def test_compiled_briefing_does_not_overwrite_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    target = CompiledBriefingTarget(
        domain="projects",
        title="Demo Project",
        slug="demo-project",
        description="Demo snippet",
        reason="reason",
    )

    def payload(current_state: str) -> str:
        return json.dumps(
            {
                "description": "Demo snippet",
                "status": "active",
                "freshness_state": "fresh",
                "confidence": "medium",
                "current_state": current_state,
                "recent_changes": ["Обновлён briefing."],
                "open_loops": ["Проверить следующий шаг."],
                "key_decisions": ["Оставить compiled note."],
                "next_check": "После следующего апдейта.",
                "source_links": ["daily/2025-04-30.md"],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        service.runner,
        "run",
        lambda _prompt, *, timeout: payload("Initial state."),
    )
    rel_path = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2025-04-30.md",
        source_excerpt="# 2025-04-30\n\n## 09:00 [text]\nInitial update.",
        signal=None,
    )
    note_path = vault_path / rel_path
    initial_content = note_path.read_text(encoding="utf-8")
    concurrent_content = initial_content.replace(
        "Initial state.",
        "Concurrent state.",
    )
    assert concurrent_content != initial_content

    def concurrent_run(_prompt: str, *, timeout: int) -> str:
        del timeout
        write_validated_vault_markdown(
            vault_path,
            note_path,
            concurrent_content.encode("utf-8"),
            manifest=service._manifest(),
        )
        return payload("Stale generated state.")

    monkeypatch.setattr(service.runner, "run", concurrent_run)
    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(
        service,
        "_resolve_targets",
        lambda **_kwargs: [target],
    )
    monkeypatch.setattr(
        service.qmd,
        "_memory_signal_for_rel_path",
        lambda _path: None,
    )

    result = service.refresh_after_write(
        source_path="daily/2025-04-30.md",
        source_excerpt="# 2025-04-30\n\n## 09:15 [text]\nStale update.",
    )

    assert note_path.read_text(encoding="utf-8") == concurrent_content
    assert result["updated"] == []
    assert result["errors"] == [
        "projects/demo-project: compiled briefing changed during build: "
        "compiled/projects/demo-project.md"
    ]


def test_compiled_decision_renders_adr_fields(tmp_path: Path) -> None:
    service = _compiled_service(tmp_path / "vault")
    target = CompiledBriefingTarget(
        domain="decisions",
        title="Choose encrypted preflight snapshots",
        slug="encrypted-preflight-snapshots",
        description="Decision about vault rollback protection.",
        reason="Durable architecture decision.",
    )

    rendered = service._render_briefing(
        target=target,
        payload={
            "record_kind": "decision",
            "decision_status": "accepted",
            "decision_owner": "Иван",
            "decision_date": "2026-04-04",
            "rationale": "Snapshot создаётся до write-heavy цикла.",
            "alternatives_considered": ["Только git", "Только host backup"],
            "supersedes": [],
            "superseded_by": "",
            "decision_evidence": ["daily/2026-04-04.md"],
            "current_state": "Решение принято.",
            "key_decisions": ["Использовать GPG."],
            "source_links": ["daily/2026-04-04.md"],
        },
        source_rel_path="daily/2026-04-04.md",
        existing_text="",
        existing_meta={},
        signal=None,
    )

    assert "record_kind: decision" in rendered
    assert "decision_status: accepted" in rendered
    assert "## Decision Record" in rendered
    assert "## Rationale\nSnapshot создаётся" in rendered
    assert "## Alternatives Considered\n- Только git" in rendered
    assert "## Decision Evidence\n- [[daily/2026-04-04.md]]" in rendered


def test_compiled_decision_preserves_accepted_record_without_supersession(
    tmp_path: Path,
) -> None:
    service = _compiled_service(tmp_path / "vault")
    target = CompiledBriefingTarget(
        domain="decisions",
        title="Stable decision",
        slug="stable-decision",
        description="Stable accepted decision.",
        reason="New source.",
    )
    existing = (
        "---\nrecord_kind: decision\ndecision_status: accepted\n"
        "decision_owner: Иван\ndecision_date: 2026-04-01\n---\n\n"
        "# Stable decision\n\n## Rationale\nИсходное обоснование.\n\n"
        "## Alternatives Considered\n- Исходная альтернатива.\n\n"
        "## Key Decisions\n- Исходное принятое решение.\n"
    )

    rendered = service._render_briefing(
        target=target,
        payload={
            "record_kind": "incident",
            "decision_status": "rejected",
            "rationale": "Переписанное обоснование.",
            "alternatives_considered": ["Новая альтернатива."],
            "key_decisions": ["Переписанное решение."],
            "current_state": "Появился новый источник.",
        },
        source_rel_path="daily/2026-04-04.md",
        existing_text=existing,
        existing_meta=service._frontmatter_fields(existing),
        signal=None,
    )

    assert "decision_status: accepted" in rendered
    assert "Исходное обоснование." in rendered
    assert "Исходная альтернатива." in rendered
    assert "Исходное принятое решение." in rendered
    assert "Переписанное обоснование." not in rendered


def test_compiled_incident_renders_debrief_fields(tmp_path: Path) -> None:
    service = _compiled_service(tmp_path / "vault")
    target = CompiledBriefingTarget(
        domain="decisions",
        title="Scheduled cycle incident",
        slug="scheduled-cycle-incident",
        description="Debrief for a scheduled-cycle failure.",
        reason="Recurring operational learning.",
    )

    rendered = service._render_briefing(
        target=target,
        payload={
            "record_kind": "incident",
            "incident_date": "2026-04-04",
            "severity": "medium",
            "timeline": ["12:00 — цикл запущен", "12:03 — запись остановлена"],
            "root_cause": "Ошибка шифрования snapshot.",
            "what_worked": ["Preflight остановил запись."],
            "what_did_not_work": ["Ключ получателя устарел."],
            "corrective_actions": ["Обновить ключ."],
            "generalizable_learning": "Проверять ключ до архивации.",
            "current_state": "Инцидент закрыт.",
        },
        source_rel_path="daily/2026-04-04.md",
        existing_text="",
        existing_meta={},
        signal=None,
    )

    assert "record_kind: incident" in rendered
    assert "## Incident Debrief" in rendered
    assert "## Timeline\n- 12:00 — цикл запущен" in rendered
    assert "## Root Cause\nОшибка шифрования snapshot." in rendered
    assert "## Corrective Actions\n- Обновить ключ." in rendered
    assert "## Generalizable Learning\nПроверять ключ" in rendered


def test_compiled_briefings_refresh_daily_fully_stops_on_terminal_backend_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    daily_path = vault_path / "daily" / "2025-05-03.md"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text(
        (
            "# 2025-05-03\n\n"
            "## 09:00 [text]\n"
            "Первый блок.\n\n"
            "## 10:00 [text]\n"
            "Второй блок.\n\n"
            "## 11:00 [text]\n"
            "Третий блок.\n"
        ),
        encoding="utf-8",
    )
    service = _compiled_service(vault_path)
    seen_excerpts: list[str] = []

    def fake_refresh(self, *, source_path, source_excerpt="", max_updates=3):  # noqa: ANN001
        del self, source_path, max_updates
        seen_excerpts.append(source_excerpt)
        if len(seen_excerpts) == 1:
            return {
                "available": True,
                "updated": [],
                "errors": [
                    "Qwen OAuth quota exceeded: Your free daily quota has been reached."
                ],
            }
        return {"available": True, "updated": [], "errors": []}

    monkeypatch.setattr(CompiledBriefingService, "refresh_after_write", fake_refresh)

    result = service.refresh_daily_fully(source_path="daily/2025-05-03.md")

    assert result["chunks"] == 3
    assert result["processed_chunks"] == 1
    assert len(seen_excerpts) == 1
    assert result["errors"] == [
        "chunk 1: Qwen OAuth quota exceeded: Your free daily quota has been reached."
    ]


def test_compiled_briefings_split_long_line_on_word_boundaries() -> None:
    source = " ".join(f"token{i}" for i in range(1, 1200))

    chunks = CompiledBriefingService._split_long_line(source, 120)

    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert all(not chunk.endswith("tok") for chunk in chunks)
    assert all(not chunk.startswith("ken") for chunk in chunks[1:])
    assert " ".join(chunks).split() == source.split()


def test_compiled_briefings_impact_timeout_extended() -> None:
    assert IMPACT_TIMEOUT_SECONDS == 360


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
