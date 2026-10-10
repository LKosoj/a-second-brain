"""Tests for the daily compiled-enrichment digest (ТЗ 7.1).

``build_daily_digest`` is pure and read-only (see module docstring), so most
tests here only assemble a temporary ``compiled/**`` tree and an optional
``.session/decisions-queue.json`` -- no vault write path is exercised.

CLI tests for ``run_compiled_digest.main`` monkeypatch
``write_validated_vault_markdown``/``send_telegram_text_sync`` instead of
calling them for real: in this sandbox, the real
``write_validated_vault_markdown`` fails with
``UnsafeVaultPathError: vault Markdown parent does not exist`` even with a
valid manifest and an existing parent directory (confirmed by hand before
writing these tests) -- an environment limitation (see the task's report),
not something these tests should assert as correct behavior.
"""

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain import run_compiled_digest
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefings import CompiledBriefingCandidate
from d_brain.services.compiled_enrich_report import (
    MAX_CHANGE_FACTS_SHOWN,
    MAX_CHANGES_SHOWN,
    MAX_DECISIONS_SHOWN,
    STALE_REVISIT_DAYS,
    PassStatus,
    _ChangeItem,
    _collect_changes,
    _collect_revisit,
    _read_decisions_queue,
    build_daily_digest,
    collect_weekly_review,
    read_pass_status,
)
from d_brain.services.decisions_queue import (
    CONFLICT_KIND,
    list_queue_items,
)
from d_brain.services.frontmatter import parse_frontmatter_bytes, validate_document

DAY = date(2026, 8, 5)


def test_daily_digest_limits_decisions_and_changes(
    tmp_path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    decisions = []
    for index in range(MAX_DECISIONS_SHOWN + 2):
        decisions.append(
            {
                "kind": "entity-identity",
                "page": f"compiled/topics/decision-{index}.md",
                "summary": f"решение {index}",
                "since": DAY.isoformat(),
            }
        )
    _write_queue(vault, decisions)
    for index in range(MAX_CHANGES_SHOWN + 2):
        _write_page(
            vault,
            f"compiled/topics/change-{index}.md",
            domain="topics",
            title=f"Изменение {index}",
            created=DAY.isoformat(),
            last_accessed=DAY.isoformat(),
            sources_rows=[
                (DAY.isoformat(), f"daily/source-{index}.md", f"факт {index}")
            ],
        )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "Ещё вопросов: 2" in digest
    assert "Обновлено карточек: 7" in digest
    assert "решение 4" in digest
    assert "решение 5" not in digest
    assert "Изменение 4" in digest
    assert "Изменение 5" not in digest


def _compiled_page_text(
    *,
    domain: str,
    title: str,
    created: str,
    last_accessed: str,
    sources_rows: list[tuple[str, str, str]],
    conflicts_rows: list[tuple[str, str, str, str, str]] | None = None,
    claim_history_rows: list[tuple[str, str, str, str]] | None = None,
    tier: str = "active",
) -> str:
    """Render one ``compiled/**`` page in exactly the shape
    ``CompiledBriefingService`` itself writes and parses (frontmatter +
    "Sources That Shaped This Page" [+ "Claim History"] [+ "Open Conflicts"]
    tables), so the real row parsers (``_sources_shaped_rows``/
    ``_claim_history_rows``/``_open_conflicts_rows``) exercise real regexes
    instead of a hand-rolled shortcut.
    """
    frontmatter = (
        "---\n"
        "type: compiled-briefing\n"
        f"domain: {domain}\n"
        f'description: "{title}"\n'
        "status: active\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        "freshness_state: fresh\n"
        "confidence: high\n"
        f"last_accessed: {last_accessed}\n"
        "relevance: 0.80\n"
        f"tier: {tier}\n"
        "---\n\n"
    )
    body = [f"# {title}", "", "## Sources That Shaped This Page", ""]
    body += ["| Date | Source | What Added |", "| --- | --- | --- |"]
    body += [
        f"| {row_date} | [[{source}]] | {what} |"
        for row_date, source, what in sources_rows
    ]
    if claim_history_rows:
        body += ["", "## Claim History", ""]
        body += [
            "| Date | Source | Claim | Superseded By |",
            "| --- | --- | --- | --- |",
        ]
        body += [
            f"| {row_date} | [[{old_source}]] | {claim} | [[{new_source}]] |"
            for row_date, old_source, claim, new_source in claim_history_rows
        ]
    if conflicts_rows:
        body += ["", "## Open Conflicts", ""]
        body += [
            "| Date | Existing Claim | Existing Source | New Claim | New Source |",
            "| --- | --- | --- | --- | --- |",
        ]
        body += [
            f"| {row_date} | {existing_claim} | [[{existing_source}]] | "
            f"{new_claim} | [[{new_source}]] |"
            for row_date, existing_claim, existing_source, new_claim, new_source in (
                conflicts_rows
            )
        ]
    return frontmatter + "\n".join(body) + "\n"


def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compiled_page_text(**kwargs), encoding="utf-8")  # type: ignore[arg-type]


def _write_queue(vault: Path, items: list[dict[str, str]]) -> None:
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "decisions-queue.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )


# --- build_daily_digest: resilience to a malformed page -----------------


def test_broken_page_frontmatter_does_not_crash_digest(tmp_path, write_vault_manifest):
    """ТЗ 7.1: a failed pass must still yield a message with a reason -- one
    malformed page in ``compiled/`` (missing closing ``---``) must not take
    down the whole digest build. ``CompiledBriefingService._iter_candidates``
    already tolerates this for directory traversal; ``_page_fields`` must
    too, instead of raising ``FrontmatterError`` via the strict parser."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    broken_path = vault / "compiled" / "topics" / "broken.md"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text(
        "---\n"
        "type: compiled-briefing\n"
        "domain: topics\n"
        "# Сломанная страница\n\n"
        "## Sources That Shaped This Page\n\n"
        "| Date | Source | What Added |\n"
        "| --- | --- | --- |\n"
        f"| {DAY.isoformat()} | [[daily/2026-08-05.md]] | сломанная запись |\n",
        encoding="utf-8",
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "compiled/topics/aurora.md" in digest


# --- build_daily_digest: suppression -----------------------------------


def test_no_work_and_everything_empty_returns_none(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is None


def test_missing_compiled_directory_returns_none_for_no_work(
    tmp_path, write_vault_manifest
):
    """``_iter_candidates`` degrades to an empty list for a vault that has
    never had a single compiled page yet (see its own early ``exists()``
    check) -- this must not be treated as an error."""
    vault = tmp_path / "vault"
    vault.mkdir()
    write_vault_manifest(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is None


def test_no_work_with_pages_changed_today_still_builds(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/projects/demo-project.md",
        title="Demo Project",
        domain="projects",
        created="2026-07-01",
        last_accessed=DAY.isoformat(),
        sources_rows=[
            (
                DAY.isoformat(),
                f"daily/{DAY.isoformat()}.md",
                "Новый дедлайн — 15 сентября.",
            )
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "**Обновления памяти" in digest
    assert "[[compiled/projects/demo-project.md|Demo Project]]" in digest
    assert "Новый дедлайн — 15 сентября." in digest


def test_no_work_with_queued_decision_still_builds(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "entity-identity",
                "page": "compiled/topics/aurora.md",
                "summary": "Какой проект Аврора имеется в виду?",
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "**Требует вашего ответа**" in digest
    assert "[[compiled/topics/aurora.md]]" in digest
    assert "Какой проект Аврора имеется в виду?" in digest


# --- build_daily_digest: "Что изменилось" -------------------------------


def test_changed_block_shows_current_details_without_labels(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "первая версия")],
    )
    _write_page(
        vault,
        "compiled/people/sokolova.md",
        domain="people",
        title="Мария Соколова",
        created="2026-07-01",
        last_accessed=DAY.isoformat(),
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "первый контакт"),
            (DAY.isoformat(), "daily/2026-08-05.md", "новая деталь о встрече"),
        ],
    )
    _write_page(
        vault,
        "compiled/topics/legacy.md",
        domain="topics",
        title="Старая тема",
        created="2026-06-01",
        last_accessed="2026-06-01",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "давняя запись")],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "**Обновления памяти" in digest
    assert "compiled/topics/aurora.md" in digest
    assert "новая страница" not in digest
    assert "compiled/people/sokolova.md" in digest
    assert "обогащена" not in digest
    assert "новая деталь о встрече" in digest
    assert "compiled/topics/legacy.md" not in digest


def test_enriched_change_shows_one_fact_without_history(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/budget.md",
        domain="decisions",
        title="Бюджет проекта Аврора",
        created="2026-07-01",
        last_accessed=DAY.isoformat(),
        # The superseded row was already moved out of the sources table by
        # the compile pass -- only the new (winning) claim remains here.
        sources_rows=[
            (DAY.isoformat(), "daily/2026-08-05.md", "бюджет 150k"),
            (DAY.isoformat(), "daily/2026-08-05.md", "срок согласования — пятница"),
        ],
        claim_history_rows=[
            (
                "2026-07-01",
                "daily/2026-07-01.md",
                "бюджет 100k",
                "daily/2026-08-05.md",
            )
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "бюджет 150k" in digest
    assert "срок согласования — пятница" not in digest
    assert "бюджет 100k" not in digest
    assert "Замена: было" not in digest


def test_created_date_formats_do_not_affect_short_digest(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.strftime("%Y%m%d"),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "первая версия")],
    )
    _write_page(
        vault,
        "compiled/topics/garbled.md",
        domain="topics",
        title="Сломанная дата",
        created="не дата",
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    lines = digest.splitlines()
    aurora_line = next(line for line in lines if "compiled/topics/aurora.md" in line)
    garbled_line = next(line for line in lines if "compiled/topics/garbled.md" in line)
    assert "первая версия" in aurora_line
    assert "обновление" in garbled_line


# --- build_daily_digest: "Требует решения" / conflicts + queue ---------


def test_digest_counts_only_what_the_queue_screen_shows(tmp_path, write_vault_manifest):
    """The digest's decision count and the "Очередь" screen must agree.

    Both conflict-pointer kinds are hidden by ``list_queue_items`` (the
    conflict they point at is already listed from the page itself), so
    counting them here sent the owner to a screen with nothing on it -- one
    renamed page left a "Требует решения — ещё 1 пункт" line that could
    never be answered.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "blocked-action",
                "page": "compiled/decisions/budget.md",
                "summary": "замена заблокирована уровнем доверия",
                "since": DAY.isoformat(),
            },
            {
                "kind": "undecided-conflict",
                "page": "compiled/decisions/hiring.md",
                "summary": "модель не смогла выбрать между версиями",
                "since": DAY.isoformat(),
            },
        ],
    )

    assert _read_decisions_queue(vault) == []
    assert list_queue_items(vault) == []
    # Nothing else happened, so with both entries hidden this is a quiet day.
    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))
    assert digest is None


def test_decisions_queue_missing_file_is_empty_not_error(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)

    items = _read_decisions_queue(vault)

    assert items == []


def test_decisions_queue_corrupt_json_is_empty_not_error(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "decisions-queue.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    items = _read_decisions_queue(vault)

    assert items == []


def test_decisions_queue_non_list_payload_is_empty_not_error(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "decisions-queue.json").write_text(
        json.dumps({"not": "a list"}), encoding="utf-8"
    )

    items = _read_decisions_queue(vault)

    assert items == []


# --- build_daily_digest: "Стоит посмотреть" -----------------------------


def test_daily_digest_omits_revisit_even_with_related_stale_pages(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora-today.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    # Related by title word ("аврора"), stale (>= 90 days) and cold tier.
    _write_page(
        vault,
        "compiled/topics/aurora-strategy.md",
        domain="topics",
        title="Стратегия Аврора 2025",
        created="2025-01-01",
        last_accessed="2026-01-01",
        sources_rows=[("2025-01-01", "daily/2025-01-01.md", "план")],
        tier="cold",
    )
    # Stale but unrelated (different domain, different title words).
    _write_page(
        vault,
        "compiled/people/unrelated.md",
        domain="people",
        title="Иван Петров",
        created="2025-01-01",
        last_accessed="2026-01-01",
        sources_rows=[("2025-01-01", "daily/2025-01-01.md", "контакт")],
        tier="cold",
    )
    # Related by title but recently accessed -- not stale.
    _write_page(
        vault,
        "compiled/topics/aurora-recent.md",
        domain="topics",
        title="Заметки Аврора",
        created="2026-08-01",
        last_accessed="2026-08-01",
        sources_rows=[("2026-08-01", "daily/2026-08-01.md", "заметка")],
        tier="cold",
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "**Стоит посмотреть**" not in digest
    assert "compiled/topics/aurora-strategy.md" not in digest
    assert "compiled/people/unrelated.md" not in digest
    assert "compiled/topics/aurora-recent.md" not in digest


def test_worth_a_look_omitted_entirely_when_no_candidate(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "Стоит посмотреть" not in digest


def _revisit_candidate(
    rel_path: str, title: str, domain: str, tier: str, last_accessed: str
) -> CompiledBriefingCandidate:
    text = f"---\nlast_accessed: {last_accessed}\n---\n\n# {title}\n"
    return CompiledBriefingCandidate(
        rel_path=rel_path,
        domain=domain,
        slug=Path(rel_path).stem,
        title=title,
        description="",
        freshness_state="",
        confidence="",
        relevance=0.0,
        tier=tier,
        text=text,
    )


def test_collect_revisit_ignores_non_cold_tier(tmp_path):
    """ТЗ 6.1/6.5: only ``cold``/``archive`` tier pages resurface -- a page
    still at a warmer tier is in active use and is not "forgotten", even if
    it is stale and related by title."""
    changes = [
        _ChangeItem(
            rel_path="compiled/topics/today.md",
            title="Проект Аврора",
            domain="topics",
            created_today=True,
            what_added="x",
        )
    ]
    warm = _revisit_candidate(
        "compiled/topics/warm.md",
        "Стратегия Аврора Warm",
        "topics",
        "warm",
        "2026-01-01",
    )
    cold = _revisit_candidate(
        "compiled/topics/cold.md",
        "Стратегия Аврора Cold",
        "topics",
        "cold",
        "2026-01-01",
    )

    picks = _collect_revisit([warm, cold], changes, DAY)

    picked_paths = {rel_path for rel_path, _ in picks}
    assert "compiled/topics/cold.md" in picked_paths
    assert "compiled/topics/warm.md" not in picked_paths


def test_collect_revisit_staleness_boundary_is_exactly_stale_revisit_days():
    """ТЗ 5.6: "нет обращений 90 дней" -- a page last opened exactly
    ``STALE_REVISIT_DAYS`` ago qualifies; one day younger does not. Neither
    side of that boundary was pinned before (a ``<`` → ``<=`` mutation of
    the check passed the whole suite)."""
    changes = [
        _ChangeItem(
            rel_path="compiled/topics/today.md",
            title="Проект Аврора",
            domain="topics",
            created_today=True,
            what_added="x",
        )
    ]
    exactly_stale = _revisit_candidate(
        "compiled/topics/stale.md",
        "Аврора Stale",
        "topics",
        "cold",
        (DAY - timedelta(days=STALE_REVISIT_DAYS)).isoformat(),
    )
    one_day_fresher = _revisit_candidate(
        "compiled/topics/fresher.md",
        "Аврора Fresher",
        "topics",
        "cold",
        (DAY - timedelta(days=STALE_REVISIT_DAYS - 1)).isoformat(),
    )

    picks = _collect_revisit([exactly_stale, one_day_fresher], changes, DAY)

    picked_paths = {rel_path for rel_path, _ in picks}
    assert picked_paths == {"compiled/topics/stale.md"}


def test_collect_revisit_deterministic_per_day_but_varies_across_days():
    """ТЗ 6.5: the pick must reproduce for a same-day rerun but must not
    always be the same page regardless of date (e.g. alphabetically first)
    -- it varies deterministically with the digest date via a stable hash."""
    changes = [
        _ChangeItem(
            rel_path="compiled/topics/today.md",
            title="Проект Аврора",
            domain="topics",
            created_today=True,
            what_added="x",
        )
    ]
    candidates = [
        _revisit_candidate(
            f"compiled/topics/aurora-{letter}.md",
            f"Аврора {letter}",
            "topics",
            "cold",
            "2026-01-01",
        )
        for letter in "abc"
    ]

    first = _collect_revisit(candidates, changes, DAY)
    second = _collect_revisit(candidates, changes, DAY)
    other_day_picks = _collect_revisit(candidates, changes, date(2026, 8, 20))

    assert first == second
    assert len(first) == 2
    assert first != other_day_picks
    expected_first = sorted(
        (c.rel_path for c in candidates),
        key=lambda p: hashlib.sha256(f"{DAY.isoformat()}|{p}".encode()).hexdigest(),
    )[:2]
    assert [rel_path for rel_path, _ in first] == expected_first


# --- read_pass_status: journal reading (задача N) ------------------------


def _write_journal(vault: Path, payload: dict) -> None:
    journal = vault / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_pass_status_missing_journal_falls_back_to_success(tmp_path):
    status = read_pass_status(tmp_path)

    assert status.status == "success"
    assert status.error == ""


def test_read_pass_status_reads_real_ok_journal(tmp_path):
    _write_journal(tmp_path, {"status": "ok"})

    status = read_pass_status(tmp_path)

    assert status.status == "ok"
    assert status.error == ""


def test_read_pass_status_reads_real_no_work_journal(tmp_path):
    _write_journal(tmp_path, {"status": "no-work"})

    status = read_pass_status(tmp_path)

    assert status.status == "no-work"
    assert status.error == ""


def test_read_pass_status_reads_real_failed_journal_with_error(tmp_path):
    _write_journal(tmp_path, {"status": "failed", "error": "qmd unavailable"})

    status = read_pass_status(tmp_path)

    assert status.status == "failed"
    assert status.error == "qmd unavailable"


def test_read_pass_status_failed_journal_with_missing_error_field(tmp_path):
    """Задача N дефект 2: the journal writer always fills ``error`` today,
    but this reader is documented as tolerant of a corrupted/hand-edited
    journal -- a ``"failed"`` status with no ``error`` key at all must not
    crash and must come back as an empty string, not e.g. ``None``."""
    _write_journal(tmp_path, {"status": "failed"})

    status = read_pass_status(tmp_path)

    assert status.status == "failed"
    assert status.error == ""


def test_read_pass_status_failed_journal_with_non_string_error_field(tmp_path):
    """Same tolerance, for an ``error`` field that is present but not a
    string (e.g. a number) -- must be coerced to an empty string rather
    than crashing or being passed through as-is."""
    _write_journal(tmp_path, {"status": "failed", "error": 12345})

    status = read_pass_status(tmp_path)

    assert status.status == "failed"
    assert status.error == ""


def test_read_pass_status_corrupt_json_is_unknown_and_logs_warning(tmp_path, caplog):
    """Код-ревью дефект: a journal that *exists* but is not valid JSON at
    all is not the same situation as a *missing* journal -- it is a sign
    something went wrong (manual edit, filesystem corruption, a writer
    bug), and must not silently read as ``success``. ``read_pass_status``
    must not crash on it (still shared by the CLI, the bot's "Дайджест"
    button, and the ``maintenance.compiled-digest`` nightly step), but it
    must report the honest ``"unknown"`` status and log a warning so the
    signal is not lost even if nobody looks at that day's digest."""
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        status = read_pass_status(tmp_path)

    assert status.status == "unknown"
    assert status.error == ""
    assert any("compile-enrich.json" in record.message for record in caplog.records)


def test_read_pass_status_list_payload_is_unknown_and_logs_warning(tmp_path, caplog):
    """Valid JSON that is not an object (e.g. a bare list) is not the
    documented shape either -- same "unknown", not "success", and the same
    warning, as the fully-corrupt-JSON case above."""
    journal = tmp_path / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(["ok"]), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        status = read_pass_status(tmp_path)

    assert status.status == "unknown"
    assert status.error == ""
    assert any("compile-enrich.json" in record.message for record in caplog.records)


def test_read_pass_status_object_without_status_field_is_unknown_and_logs_warning(
    tmp_path, caplog
):
    """An object with no ``status`` key at all (hand-edited or written by a
    future journal version) must not crash and must not surface an
    empty/``None`` status -- but it also must not be reported as
    ``success``, since a journal we could not make sense of is not evidence
    the pass actually succeeded."""
    _write_journal(tmp_path, {"error": "something"})

    with caplog.at_level(logging.WARNING):
        status = read_pass_status(tmp_path)

    assert status.status == "unknown"
    assert status.error == ""
    assert any("compile-enrich.json" in record.message for record in caplog.records)


def test_read_pass_status_no_work_suppresses_quiet_digest(
    tmp_path, write_vault_manifest
):
    """Same "runs, not reasons" verification as the test above, for the one
    status that must suppress: a real "no-work" journal on an otherwise
    quiet vault reaches ``build_daily_digest`` and does trip the
    suppression, exactly like the hand-built ``PassStatus`` used elsewhere
    in this file."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_journal(vault, {"status": "no-work"})

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is None


# --- budget_exhausted: ТЗ 5.5 инв 7 "факт исчерпания попадает в дайджест",
# "молчаливое усечение запрещено" (задача N дефект 1) ----------------------


def test_read_pass_status_reads_budget_exhausted_list(tmp_path):
    _write_journal(
        tmp_path,
        {
            "status": "ok",
            "budget_exhausted": ["pages-per-pass", "model-calls-per-pass"],
        },
    )

    status = read_pass_status(tmp_path)

    assert status.budget_exhausted == ("pages-per-pass", "model-calls-per-pass")


def test_read_pass_status_missing_budget_exhausted_field_is_empty_tuple(tmp_path):
    _write_journal(tmp_path, {"status": "ok"})

    status = read_pass_status(tmp_path)

    assert status.budget_exhausted == ()


def test_read_pass_status_non_list_budget_exhausted_field_is_empty_tuple(tmp_path):
    """Tolerant like every other field this reader parses: a hand-edited or
    corrupted journal whose ``budget_exhausted`` is not a list at all must
    not crash and must not be treated as one exhausted budget."""
    _write_journal(tmp_path, {"status": "ok", "budget_exhausted": "pages-per-pass"})

    status = read_pass_status(tmp_path)

    assert status.budget_exhausted == ()


def test_read_pass_status_filters_non_string_and_blank_budget_entries(tmp_path):
    _write_journal(
        tmp_path,
        {"status": "ok", "budget_exhausted": ["pages-per-pass", 42, "   ", None]},
    )

    status = read_pass_status(tmp_path)

    assert status.budget_exhausted == ("pages-per-pass",)


def test_read_pass_status_reads_monthly_capped_pages_and_deferred_sources(tmp_path):
    """The two fields that make a monthly-cap budget line actionable: which
    page ran out of enrichments, and how much source material the pass left
    in the queue behind it."""
    _write_journal(
        tmp_path,
        {
            "status": "ok",
            "budget_exhausted": ["monthly-enrichments-per-page"],
            "monthly_capped_pages": ["compiled/projects/demo.md"],
            "deferred_queue_sources": 4,
        },
    )

    status = read_pass_status(tmp_path)

    assert status.monthly_capped_pages == ("compiled/projects/demo.md",)
    assert status.deferred_queue_sources == 4


def test_read_pass_status_tolerates_bad_monthly_cap_fields(tmp_path):
    """Same tolerance as every other field this reader parses: a journal
    written before these fields existed, or one hand-edited into the wrong
    shape, must degrade to "unknown" rather than crash or invent a page."""
    _write_journal(
        tmp_path,
        {
            "status": "ok",
            "monthly_capped_pages": "compiled/projects/demo.md",
            "deferred_queue_sources": "many",
        },
    )

    status = read_pass_status(tmp_path)

    assert status.monthly_capped_pages == ()
    assert status.deferred_queue_sources == 0


def test_budget_exhausted_labels_cover_every_name_the_core_can_write():
    """Задача N дефект 1: cross-check the full set of technical constraint
    names ``compiled_briefings.py`` (the core -- out of this task's edit
    zone, read-only) may actually record in the pass journal's
    ``budget_exhausted`` field against this module's translation table. If
    the core ever adds a new ``pass_obj.budget_exhausted.add("...")`` call
    site without a matching entry here, this test must fail instead of
    letting an untranslated technical name reach the owner silently via the
    unknown-name fallback. ``MONTHLY_CAP_BUDGET_NAME`` is translated by
    ``_describe_monthly_cap`` instead of the table, because its sentence
    carries the capped pages' paths, so it counts as covered too."""
    import inspect
    import re

    from d_brain.services import compiled_briefings
    from d_brain.services.compiled_enrich_report import (
        _BUDGET_EXHAUSTED_LABELS,
        MONTHLY_CAP_BUDGET_NAME,
    )

    source = inspect.getsource(compiled_briefings)
    names = set(re.findall(r'budget_exhausted\.add\("([^"]+)"\)', source))

    assert names, "no budget_exhausted.add(...) call sites found in the core"
    assert names <= set(_BUDGET_EXHAUSTED_LABELS) | {MONTHLY_CAP_BUDGET_NAME}


def test_describe_budget_exhausted_unknown_name_is_non_empty_and_safe():
    """ТЗ acceptance: an unrecognized budget name (e.g. one added to the
    core after this table was last updated, or a hand-edited journal) must
    not crash and must not render as an empty string -- it gets a generic
    but still-readable sentence that keeps the raw name visible instead of
    hiding it."""
    from d_brain.services.compiled_enrich_report import describe_budget_exhausted

    lines = describe_budget_exhausted(("some-future-budget-name",))

    assert len(lines) == 1
    assert lines[0].strip()
    assert "some-future-budget-name" in lines[0]


# --- queue_evictions: ТЗ 7.2 "факт вытеснения попадает в дайджест"
# (code review defect 2) ---------------------------------------------------


def test_read_pass_status_reads_queue_evictions_int(tmp_path):
    _write_journal(tmp_path, {"status": "ok", "queue_evictions": 3})

    status = read_pass_status(tmp_path)

    assert status.queue_evictions == 3


def test_read_pass_status_missing_queue_evictions_field_is_zero(tmp_path):
    _write_journal(tmp_path, {"status": "ok"})

    status = read_pass_status(tmp_path)

    assert status.queue_evictions == 0


def test_read_pass_status_non_positive_or_non_int_queue_evictions_is_zero(tmp_path):
    """Tolerant like every other field this reader parses: a hand-edited or
    corrupted journal's ``queue_evictions`` must never crash and must never
    be treated as a real eviction count unless it is a genuine positive
    ``int`` -- a bare ``bool`` (``isinstance(True, int)`` is ``True`` in
    Python) must not slip through as 0 or 1 evictions either."""
    for bad_value in (0, -1, "3", 1.5, True, None, []):
        _write_journal(tmp_path, {"status": "ok", "queue_evictions": bad_value})

        status = read_pass_status(tmp_path)

        assert status.queue_evictions == 0, bad_value


# --- human_zone_ambiguous_pages: owner-visibility gap fix (code review) --
# Pages ``compiled_briefings.py`` skips because their <!-- human:start/end
# --> markers are ambiguous used to only reach the server logs -- the owner
# who has to fix the marker never learned about it. Mirrors
# ``budget_exhausted``/``queue_evictions`` above.


def test_read_pass_status_reads_human_zone_ambiguous_pages_list(tmp_path):
    _write_journal(
        tmp_path,
        {
            "status": "ok",
            "human_zone_ambiguous_pages": [
                "compiled/projects/a.md",
                "compiled/projects/b.md",
            ],
        },
    )

    status = read_pass_status(tmp_path)

    assert status.human_zone_ambiguous_pages == (
        "compiled/projects/a.md",
        "compiled/projects/b.md",
    )


def test_read_pass_status_missing_human_zone_ambiguous_pages_field_is_empty_tuple(
    tmp_path,
):
    _write_journal(tmp_path, {"status": "ok"})

    status = read_pass_status(tmp_path)

    assert status.human_zone_ambiguous_pages == ()


def test_read_pass_status_non_list_human_zone_ambiguous_pages_field_is_empty_tuple(
    tmp_path,
):
    """Tolerant like every other field this reader parses: a hand-edited or
    corrupted journal whose ``human_zone_ambiguous_pages`` is not a list at
    all must not crash and must not be treated as one skipped page."""
    _write_journal(
        tmp_path,
        {"status": "ok", "human_zone_ambiguous_pages": "compiled/projects/a.md"},
    )

    status = read_pass_status(tmp_path)

    assert status.human_zone_ambiguous_pages == ()


def test_read_pass_status_filters_non_string_and_blank_human_zone_entries(tmp_path):
    _write_journal(
        tmp_path,
        {
            "status": "ok",
            "human_zone_ambiguous_pages": ["compiled/projects/a.md", 42, "   ", None],
        },
    )

    status = read_pass_status(tmp_path)

    assert status.human_zone_ambiguous_pages == ("compiled/projects/a.md",)


# --- _collect_changes: date-range generalization (задача N) --------------


def test_collect_changes_single_day_bounds_matches_original_behavior():
    """The single-day digest call site passes ``start == end == day`` --
    this must reproduce the original one-day collector exactly: a row dated
    outside that one day is excluded, one dated on it is included."""
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/topics/aurora.md",
        domain="topics",
        slug="aurora",
        title="Проект Аврора",
        description="",
        freshness_state="",
        confidence="",
        relevance=0.0,
        tier="active",
        text=(
            "---\ncreated: 2026-08-01\n---\n\n# Проект Аврора\n\n"
            "## Sources That Shaped This Page\n\n"
            "| Date | Source | What Added |\n| --- | --- | --- |\n"
            "| 2026-08-04 | [[daily/2026-08-04.md]] | вчерашнее |\n"
            "| 2026-08-05 | [[daily/2026-08-05.md]] | сегодняшнее |\n"
        ),
    )

    changes = _collect_changes([candidate], DAY, DAY)

    assert len(changes) == 1
    assert changes[0].what_added == "сегодняшнее"


def test_collect_changes_range_widens_window_to_multiple_days():
    """A multi-day window (``start != end``) picks up rows from every day
    in the closed range, not just the last one -- the weekly-review screen's
    use case."""
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/topics/aurora.md",
        domain="topics",
        slug="aurora",
        title="Проект Аврора",
        description="",
        freshness_state="",
        confidence="",
        relevance=0.0,
        tier="active",
        text=(
            "---\ncreated: 2026-07-20\n---\n\n# Проект Аврора\n\n"
            "## Sources That Shaped This Page\n\n"
            "| Date | Source | What Added |\n| --- | --- | --- |\n"
            "| 2026-07-30 | [[daily/2026-07-30.md]] | до недели |\n"
            "| 2026-08-02 | [[daily/2026-08-02.md]] | внутри недели |\n"
            "| 2026-08-05 | [[daily/2026-08-05.md]] | конец недели |\n"
        ),
    )
    start = date(2026, 7, 31)
    end = DAY

    changes = _collect_changes([candidate], start, end)

    assert len(changes) == 1
    assert "внутри недели" in changes[0].what_added
    assert "конец недели" in changes[0].what_added
    assert "до недели" not in changes[0].what_added


def test_collect_changes_does_not_glue_a_separator_onto_a_finished_sentence():
    """Each source row's cell is already a full sentence, so the joined line
    must not read "...ограничений.; LightLLM..." in the owner's digest."""
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/topics/aurora.md",
        domain="topics",
        slug="aurora",
        title="Проект Аврора",
        description="",
        freshness_state="",
        confidence="",
        relevance=0.0,
        tier="active",
        text=(
            "---\ncreated: 2026-08-01\n---\n\n# Проект Аврора\n\n"
            "## Sources That Shaped This Page\n\n"
            "| Date | Source | What Added |\n| --- | --- | --- |\n"
            "| 2026-08-05 | [[daily/a.md]] | Первый факт. |\n"
            "| 2026-08-05 | [[daily/b.md]] | Второй факт. |\n"
            "| 2026-08-05 | [[daily/c.md]] | Третий факт без точки |\n"
            "| 2026-08-05 | [[daily/d.md]] | Четвёртый факт. |\n"
        ),
    )

    changes = _collect_changes([candidate], DAY, DAY)

    assert changes[0].what_added == (
        "Первый факт. Второй факт. Третий факт без точки; Четвёртый факт."
    )


def test_collect_changes_deduplicates_and_limits_facts():
    rows = [
        f"| {DAY.isoformat()} | [[daily/{index}.md]] | Факт {index}. |"
        for index in range(MAX_CHANGE_FACTS_SHOWN + 2)
    ]
    rows.insert(
        1,
        f"| {DAY.isoformat()} | [[daily/duplicate.md]] |  факт 0  |",
    )
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/topics/aurora.md",
        domain="topics",
        slug="aurora",
        title="Проект Аврора",
        description="",
        freshness_state="",
        confidence="",
        relevance=0.0,
        tier="active",
        text=(
            "---\ncreated: 2026-08-01\n---\n\n# Проект Аврора\n\n"
            "## Sources That Shaped This Page\n\n"
            "| Date | Source | What Added |\n| --- | --- | --- |\n"
            + "\n".join(rows)
            + "\n"
        ),
    )

    changes = _collect_changes([candidate], DAY, DAY)

    assert changes[0].what_added.count("Факт 0") == 1
    assert f"Факт {MAX_CHANGE_FACTS_SHOWN - 1}." in changes[0].what_added
    assert f"Факт {MAX_CHANGE_FACTS_SHOWN}." not in changes[0].what_added
    assert "Ещё пунктов: 2." in changes[0].what_added


# --- _collect_revisit: limit parameter (задача N) -------------------------


def test_collect_revisit_default_limit_is_two():
    changes = [
        _ChangeItem(
            rel_path="compiled/topics/today.md",
            title="Проект Аврора",
            domain="topics",
            created_today=True,
            what_added="x",
        )
    ]
    candidates = [
        _revisit_candidate(
            f"compiled/topics/aurora-{letter}.md",
            f"Аврора {letter}",
            "topics",
            "cold",
            "2026-01-01",
        )
        for letter in "abc"
    ]

    picks = _collect_revisit(candidates, changes, DAY)

    assert len(picks) == 2


def test_collect_revisit_limit_one_used_by_weekly_review():
    """ТЗ: the weekly-review screen wants exactly one forgotten page, not
    the daily digest's 1-2 -- calling with ``limit=1`` instead of adding a
    second, near-duplicate collector."""
    changes = [
        _ChangeItem(
            rel_path="compiled/topics/today.md",
            title="Проект Аврора",
            domain="topics",
            created_today=True,
            what_added="x",
        )
    ]
    candidates = [
        _revisit_candidate(
            f"compiled/topics/aurora-{letter}.md",
            f"Аврора {letter}",
            "topics",
            "cold",
            "2026-01-01",
        )
        for letter in "abc"
    ]

    picks = _collect_revisit(candidates, changes, DAY, limit=1)

    assert len(picks) == 1


# --- collect_weekly_review (задача N, missed acceptance criterion) --------


def test_collect_weekly_review_queue_matches_list_queue_items(
    tmp_path, write_vault_manifest
):
    """Explicit requirement: the weekly screen's queue must come from the
    exact same ``list_queue_items`` the interactive queue screen uses, not
    a separate simplified merge -- so the two screens can never disagree."""
    from d_brain.services.decisions_queue import list_queue_items

    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/budget.md",
        domain="decisions",
        title="Бюджет проекта Аврора",
        created="2026-07-01",
        last_accessed="2026-07-01",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "первичный бюджет")],
        conflicts_rows=[
            (
                "2026-08-01",
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-08-01.md",
            )
        ],
    )

    review = collect_weekly_review(vault, DAY)

    assert list(review.queue_items) == list_queue_items(vault)
    assert len(review.queue_items) == 1
    assert review.queue_items[0].kind == CONFLICT_KIND


def test_collect_weekly_review_window_and_review_pick(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created="2026-07-31",
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    _write_page(
        vault,
        "compiled/topics/other.md",
        domain="topics",
        title="Другая страница",
        created="2020-01-01",
        last_accessed="2020-01-01",
        sources_rows=[("2020-01-01", "daily/2020-01-01.md", "старое")],
    )

    review = collect_weekly_review(vault, DAY)

    assert review.start == DAY - timedelta(days=6)
    assert review.end == DAY
    assert len(review.changes) == 1
    assert review.changes[0].rel_path == "compiled/topics/aurora.md"
    assert review.review_pick == ("compiled/topics/aurora.md", "Проект Аврора")


def test_collect_weekly_review_no_changes_means_no_revisit_pick(
    tmp_path, write_vault_manifest
):
    """Known edge case, kept unchanged on purpose: ``_collect_revisit`` only
    looks for pages related to the window's own changes, so a week with no
    changes at all also has no forgotten-page suggestion -- this is not
    special-cased for the new caller."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/cold.md",
        domain="topics",
        title="Забытая страница",
        created="2020-01-01",
        last_accessed="2020-01-01",
        sources_rows=[("2020-01-01", "daily/2020-01-01.md", "старое")],
        tier="cold",
    )

    review = collect_weekly_review(vault, DAY)

    assert review.changes == ()
    assert review.revisit == ()
    assert review.review_pick is None


# --- run_compiled_digest CLI --------------------------------------------


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_digest, "get_settings", lambda: SimpleNamespace(vault_path=vault)
    )


def test_render_note_passes_write_time_frontmatter_validation(
    tmp_path, write_vault_manifest
):
    """The write path (``write_validated_vault_markdown``) is fully mocked
    in the CLI tests below because the real one cannot run in this sandbox
    (see module docstring) -- so the bytes ``_render_note`` builds are never
    actually checked against the manifest's ``derived`` profile anywhere
    else. Run them through the same validation the write path applies
    (``parse_frontmatter_bytes`` + ``validate_document``) without touching
    disk."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    manifest = load_manifest_for_vault(vault)

    content = run_compiled_digest._render_note(DAY, "**Дайджест обогащения**\n\nтест")
    document = parse_frontmatter_bytes(content)
    relative_path = f"summaries/compile/{DAY.isoformat()}.md"

    _route, missing, invalid = validate_document(relative_path, document, manifest)

    assert missing == ()
    assert invalid == ()


def test_dry_run_prints_digest_without_writing_or_sending(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    _patch_settings(monkeypatch, vault)

    def _fail_write(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not write")

    def _fail_send(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(
        run_compiled_digest, "write_validated_vault_markdown", _fail_write
    )
    monkeypatch.setattr(run_compiled_digest, "send_telegram_text_sync", _fail_send)
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"])

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "compiled/topics/aurora.md" in out
    assert not (vault / "summaries").exists()


def test_dry_run_quiet_day_prints_no_work(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"])

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no-work: nothing to report" in out


def test_dry_run_none_digest_prints_no_work_placeholder(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    """Direct unit coverage for the CLI's ``if digest is None`` branch: it
    is unreachable with the journal-less "success" fallback status (see the
    test above), but ``build_daily_digest`` is typed ``str | None`` and any
    caller of it must handle that case correctly regardless of whether the
    current default happens to trigger it."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(run_compiled_digest, "build_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"])

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no-work" in out


def test_main_writes_digest_file_and_sends_telegram(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    _patch_settings(monkeypatch, vault)

    write_calls: list[tuple[Path, bytes, dict]] = []
    send_calls: list[tuple[str, dict]] = []

    def _fake_write(vault_path, path, content, **kwargs):  # type: ignore[no-untyped-def]
        del vault_path
        write_calls.append((path, content, kwargs))

    def _fake_send(text, **kwargs):  # type: ignore[no-untyped-def]
        send_calls.append((text, kwargs))

    monkeypatch.setattr(
        run_compiled_digest, "write_validated_vault_markdown", _fake_write
    )
    monkeypatch.setattr(run_compiled_digest, "send_telegram_text_sync", _fake_send)
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat()])

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    assert len(write_calls) == 1
    path, content, kwargs = write_calls[0]
    assert path == vault / "summaries" / "compile" / f"{DAY.isoformat()}.md"
    assert b"type: compiled-digest" in content
    assert b"compiled/topics/aurora.md" in content
    assert kwargs.get("require_absent") is not True
    assert len(send_calls) == 1
    text, send_kwargs = send_calls[0]
    assert "compiled/topics/aurora.md" in text
    assert send_kwargs.get("rich") is True


def test_main_rerun_same_day_targets_same_path(
    tmp_path, write_vault_manifest, monkeypatch
):
    """ТЗ: "запись в summaries/compile/YYYY-MM-DD.md идемпотентна:
    повторный запуск в тот же день перезаписывает файл, а не плодит
    копии" -- verified at the CLI-wiring level: the same deterministic
    path is targeted on every run, unlike ``file_output_artifact``'s
    counter-suffixed unique naming."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    _patch_settings(monkeypatch, vault)
    write_calls: list[Path] = []
    monkeypatch.setattr(
        run_compiled_digest,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: write_calls.append(path),
    )
    monkeypatch.setattr(
        run_compiled_digest, "send_telegram_text_sync", lambda *a, **k: None
    )
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat()])

    assert run_compiled_digest.main() == 0
    assert run_compiled_digest.main() == 0

    assert len(write_calls) == 2
    assert write_calls[0] == write_calls[1]


def test_telegram_send_failure_is_logged_not_raised(
    tmp_path, write_vault_manifest, monkeypatch, caplog
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Проект Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_digest,
        "write_validated_vault_markdown",
        lambda *a, **k: None,
    )

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(run_compiled_digest, "send_telegram_text_sync", _boom)
    monkeypatch.setattr("sys.argv", ["prog", "--date", DAY.isoformat()])

    with caplog.at_level(logging.WARNING):
        exit_code = run_compiled_digest.main()

    assert exit_code == 0
    assert any("telegram is down" in record.message for record in caplog.records)


def _write_fact_check_journal(vault: Path, *, day: str, evictions: int) -> None:
    """The exact payload shape ``compiled_fact_check._write_fact_check_journal``
    produces, so these tests break if that writer's contract changes."""
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-fact-check.json").write_text(
        json.dumps(
            {
                "date": day,
                "finished_at": f"{day}T03:00:00+03:00",
                "pages_checked": ["compiled/topics/aurora.md"],
                "pages_patched": [],
                "pages_flagged": ["compiled/topics/aurora.md"],
                "errors": [],
                "queue_evictions": evictions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_fact_check_journal_from_another_day_is_ignored(tmp_path, write_vault_manifest):
    """That journal is monthly and is not rewritten in between, so counting
    it unconditionally would repeat the same eviction line every single day
    until the next fact-check run."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_fact_check_journal(
        vault, day=(DAY - timedelta(days=9)).isoformat(), evictions=7
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is None


def _write_queue_worker_crash_journal(
    vault: Path, *, crashed_at: str, message: str | None = "qmd index is locked"
) -> None:
    """The exact payload shape
    ``CompiledBriefingService._write_queue_worker_crash_journal`` produces, so
    these tests break if that writer's contract changes."""
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "pid": 4242,
        "started_at": f"{crashed_at[:10]}T03:00:00+03:00",
        "crashed_at": crashed_at,
        "traceback": "Traceback (most recent call last):\n  ...\n",
    }
    if message is not None:
        payload["error"] = {"type": "OSError", "message": message}
    (session / "compile-queue-worker.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_a_crash_from_another_day_is_not_repeated_forever(
    tmp_path, write_vault_manifest
):
    """That journal is overwritten per crash, not per day: counting it
    unconditionally would restate a months-old crash every morning."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue_worker_crash_journal(
        vault, crashed_at=f"{(DAY - timedelta(days=9)).isoformat()}T14:05:11+03:00"
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is None


def _write_dropped_sources_journal(vault: Path, *sources: str) -> None:
    """The exact payload shape
    ``CompiledBriefingService._record_dropped_queue_source`` produces, so
    these tests break if that writer's contract changes."""
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-dropped-sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_path": source,
                        "dropped_at": f"{DAY.isoformat()}T14:05:11+03:00",
                        "attempts": 3,
                        "errors": ["backend-refused"],
                    }
                    for source in sources
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_an_empty_dropped_sources_journal_stays_a_quiet_day(
    tmp_path, write_vault_manifest
):
    """The writer leaves ``{"sources": []}`` behind after the last dropped
    source finally compiles -- that file must not by itself force a digest
    on an otherwise silent day."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_dropped_sources_journal(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is None


@pytest.mark.parametrize("status", ["success", "ok", "no-work", "failed", "unknown"])
def test_processing_details_alone_do_not_trigger_digest(
    tmp_path, write_vault_manifest, status
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    pass_status = PassStatus(
        status=status,
        error="backend unavailable",
        budget_exhausted=("pages-per-pass", "monthly-enrichments-per-page"),
        deferred_queue_sources=2,
        queue_evictions=3,
        human_zone_ambiguous_pages=("compiled/topics/demo.md",),
        worker_crash="worker stopped",
        dropped_sources=("daily/2026-08-05.md",),
        auto_decisions=4,
    )

    assert build_daily_digest(vault, DAY, pass_status=pass_status) is None


@pytest.mark.parametrize(
    "kind",
    [
        "conflict",
        "undecided-conflict",
        "duplicate-candidate",
        "drift",
        "verify-rejected",
        "fact-check-rejected",
        "human-zone-ambiguous",
        "page-encoding-broken",
        "entity-identity",
    ],
)
@pytest.mark.parametrize("since", [DAY.isoformat(), "2026-08-04"])
def test_only_new_identity_questions_are_shown(
    tmp_path, write_vault_manifest, kind, since
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": kind,
                "page": "compiled/people/fictional-person.md",
                "summary": "Какой Иван имеется в виду?",
                "since": since,
            }
        ],
    )
    queue_before = (vault / ".session/decisions-queue.json").read_bytes()

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="ok"))

    if kind == "entity-identity" and since == DAY.isoformat():
        assert digest is not None
        assert "**Требует вашего ответа**" in digest
        assert "Какой Иван имеется в виду?" in digest
    else:
        assert digest is None
    assert (vault / ".session/decisions-queue.json").read_bytes() == queue_before


def test_open_page_conflicts_stay_out_of_digest(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/demo.md",
        domain="topics",
        title="Аврора",
        created="2026-08-01",
        last_accessed=DAY.isoformat(),
        sources_rows=[("2026-08-01", "daily/2026-08-01.md", "старый факт")],
        conflicts_rows=[
            (
                DAY.isoformat(),
                "первая версия",
                "daily/2026-08-01.md",
                "вторая версия",
                "daily/2026-08-05.md",
            )
        ],
    )
    path = vault / "compiled/topics/demo.md"
    page_before = path.read_bytes()

    assert build_daily_digest(vault, DAY, pass_status=PassStatus(status="ok")) is None
    assert path.read_bytes() == page_before


def test_new_questions_follow_changes(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/demo.md",
        domain="topics",
        title="Аврора",
        created=DAY.isoformat(),
        last_accessed=DAY.isoformat(),
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "срок — сентябрь")],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "entity-identity",
                "page": "compiled/people/ivan.md",
                "summary": "Какой Иван имеется в виду?",
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="ok"))

    assert digest is not None
    assert digest.index("срок — сентябрь") < digest.index("Требует вашего ответа")
    assert "Обновлено карточек: 1" in digest
    assert "Стоит посмотреть" not in digest
