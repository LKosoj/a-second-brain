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

from d_brain import run_compiled_digest
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefings import CompiledBriefingCandidate
from d_brain.services.compiled_enrich_report import (
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
from d_brain.services.decisions_queue import CONFLICT_KIND, QUEUE_CAP
from d_brain.services.frontmatter import parse_frontmatter_bytes, validate_document

DAY = date(2026, 8, 5)


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
    """Code review: pages are also enriched off the nightly pass, by the
    background queue drain (``refresh_after_write``). For an owner whose
    drain keeps up, the night's pass then honestly reports "no-work" while
    the day's real page changes sit on disk -- suppressing on the status
    alone hid exactly the digest's block 2 ("что изменилось", ТЗ 7.1)."""
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
    assert "**Что изменилось**" in digest
    assert "[[compiled/projects/demo-project.md|Demo Project]]" in digest
    assert "Новый дедлайн — 15 сентября." in digest


def test_no_work_with_queued_decision_still_builds(tmp_path, write_vault_manifest):
    """ТЗ 7.1: "Если проход завершился со статусом «нет работы» и очередь
    решений пуста, дайджест не отправляется" -- a non-empty queue must
    override the no-work suppression. ``since`` is today's date so this item
    lands in the "today" bucket and stays shown in full -- the carryover
    folding behavior (задача N) is covered separately, this test is only
    about the no-work override."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "duplicate-candidate",
                "page": "compiled/topics/aurora.md",
                "summary": "похоже на дубликат существующей страницы",
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "[[compiled/topics/aurora.md]]" in digest
    assert "похоже на дубликат существующей страницы" in digest


def test_no_work_with_fact_check_decision_still_builds(tmp_path, write_vault_manifest):
    """ТЗ 6.6: the monthly fact-check appends its own ``kind=
    "fact-check-rejected"`` entries to this SAME decisions queue -- proving
    no digest code change is needed for that new workflow to surface here.
    ``since`` is today's date so this item stays in the "today" (full-text)
    bucket -- see задача N's carryover-folding tests for the other case.
    """
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": (
                    "проверка фактов: 2/3 проверяемых утверждений не "
                    "подтвердились — решить, актуальна ли страница"
                ),
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "[[compiled/topics/aurora.md]]" in digest
    assert "проверка фактов" in digest


def test_error_forces_build_with_reason_first(tmp_path, write_vault_manifest):
    """ТЗ 7.1: "Сбойный проход, наоборот, всегда порождает сообщение с
    причиной" -- even under a "no-work" status, a non-empty error must
    force a digest and must be the first "Требует решения" line. ``since``
    is today's date so the queued item stays in the full-text bucket and
    this test's ordering assertion is unaffected by carryover folding
    (задача N)."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "drift",
                "page": "compiled/topics/aurora.md",
                "summary": "превышен месячный лимит обогащений",
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(
            status="no-work", error="Compile: модель вернула невалидный JSON"
        ),
    )

    assert digest is not None
    decisions_section = digest.split("**Требует решения**", 1)[1]
    error_line_index = decisions_section.index(
        "Compile: модель вернула невалидный JSON"
    )
    queue_line_index = decisions_section.index("превышен месячный лимит")
    assert error_line_index < queue_line_index


def test_failure_status_with_empty_error_still_shows_honest_fallback_line(
    tmp_path, write_vault_manifest
):
    """Задача N дефект 2: a failure status ("failed") with an empty
    ``error`` field must not render like a quiet day -- ``_render_digest``
    must fall back to an honest "reason unavailable" line instead of
    silently omitting the "Требует решения" block entirely."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="failed", error="")
    )

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "Проход завершился с ошибкой, подробности недоступны" in digest
    assert "Изменений и открытых вопросов нет." not in digest


def test_failure_status_with_non_string_error_object_does_not_crash(
    tmp_path, write_vault_manifest
):
    """Same fallback, forced via a directly constructed ``PassStatus`` whose
    ``error`` is not a string at all (bypassing ``read_pass_status``'s own
    coercion) -- ``_render_digest`` must not crash calling ``.strip()`` on
    it and must still show the honest fallback line."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(status="failed", error=None),  # type: ignore[arg-type]
    )

    assert digest is not None
    assert "Проход завершился с ошибкой, подробности недоступны" in digest


def test_success_status_with_empty_error_does_not_get_failure_fallback(
    tmp_path, write_vault_manifest
):
    """The non-regression half of the same fix: a normal successful pass
    ("success"/"ok") with an empty ``error`` and nothing else to report must
    keep showing the quiet-day fallback line, not the new failure one --
    only "no-work" is suppressed outright, but a real success must not be
    mistaken for a failure either."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    for status in ("success", "ok"):
        digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status=status))

        assert digest is not None
        assert "Изменений и открытых вопросов нет." in digest
        assert "Проход завершился с ошибкой" not in digest


# --- build_daily_digest: "Что изменилось" -------------------------------


def test_changed_block_distinguishes_created_vs_enriched(
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
    assert "**Что изменилось**" in digest
    assert "compiled/topics/aurora.md" in digest
    assert "новая страница" in digest
    assert "compiled/people/sokolova.md" in digest
    assert "обогащена" in digest
    assert "новая деталь о встрече" in digest
    assert "compiled/topics/legacy.md" not in digest


def test_enriched_change_shows_replacement_not_plain_addition(
    tmp_path, write_vault_manifest
):
    """ТЗ 7.1: for an enriched page, show a short diff, not a restatement --
    when today's row is the winning side of a temporal supersession (a
    "Claim History" row whose "Superseded By" source is today's source),
    the digest must say it replaces the prior claim and name it, not print
    the new claim as a plain addition (uses ``_claim_history_rows``)."""
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
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "бюджет 150k")],
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
    assert "бюджет 100k" in digest
    assert "замена" in digest


def test_created_field_parsed_as_date_not_raw_string(tmp_path, write_vault_manifest):
    """ТЗ: ``created`` must be parsed as a date, not compared as a raw
    string. A same-day page whose ``created`` frontmatter uses an alternate
    valid ISO date form (basic "YYYYMMDD", no dashes) is still today's page
    -- naive string equality against ``day.isoformat()`` would wrongly call
    it "enriched" instead of "new". A page with an unparsable ``created``
    value must not crash and must be treated as enriched, not new."""
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
    assert "новая страница" in aurora_line
    assert "обогащена" in garbled_line


# --- build_daily_digest: "Требует решения" / conflicts + queue ---------


def test_open_conflicts_include_both_sides(tmp_path, write_vault_manifest):
    """Uses ``status="success"`` (not "no-work") deliberately: the digest
    must always show both sides of an open conflict when it *is* built --
    the "no-work" + empty-queue combination is a suppression case covered
    separately below (see ``test_quiet_day_suppressed_despite_open_page_conflict``)
    and must not be entangled with this content assertion."""
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
                DAY.isoformat(),
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-08-05.md",
            )
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "compiled/decisions/budget.md" in digest
    assert "Бюджет 100k" in digest
    assert "daily/2026-07-01.md" in digest
    assert "Бюджет 150k" in digest
    assert "daily/2026-08-05.md" in digest


def test_open_conflicts_force_digest_even_on_a_no_work_day(
    tmp_path, write_vault_manifest
):
    """Owner acceptance correction (inverts the old
    ``test_quiet_day_suppressed_despite_open_page_conflict``, which asserted
    the opposite and was defended as intentional in this module's old
    docstrings): an open page conflict is one of the decisions queue's two
    item kinds, so it is part of the *merged* decisions list the "тишина"
    condition checks -- it must force a digest even when the pass took no
    work and reported no error, however old the conflict is.

    Its claim text is not restated in full here, though: with ``since`` a
    month before ``DAY``, it lands in the carryover bucket and is folded
    into one summary line instead (see the carryover-folding test below) --
    proving the digest is not suppressed without re-asserting the exact
    rendering of an already-old item."""
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
                "2026-07-29",
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-07-29.md",
            )
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "Ещё 1" in digest  # folded carryover summary, not full claim text


def test_decision_items_split_today_full_text_vs_carryover_summary(
    tmp_path, write_vault_manifest
):
    """ТЗ acceptance (задача N): a decision item that first appeared today
    (``since == day``) is still shown in full, exactly as before; a decision
    item that appeared on an earlier day is folded into one summary line
    with a count instead of being fully restated -- so a long-standing
    conflict or queued item does not bloat the digest every single day, but
    is also never silently dropped."""
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
                "2026-07-01",
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-07-29.md",
            )
        ],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "duplicate-candidate",
                "page": "compiled/topics/aurora.md",
                "summary": "похожа на существующую страницу",
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "похожа на существующую страницу" in digest  # today: full text
    assert "Бюджет 100k" not in digest  # carried over: folded away
    assert "Ещё 1" in digest


def test_decisions_queue_deduplicated_against_page_conflicts(
    tmp_path, write_vault_manifest
):
    """ТЗ: queue items must be merged in, "дедуплицируя по паре «страница +
    суть»" -- a queue entry describing the same page conflict must not
    double up, but a distinct queue item for that page must still show."""
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
                DAY.isoformat(),
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-08-05.md",
            )
        ],
    )
    duplicate_summary = (
        "«Бюджет 100k» (daily/2026-07-01.md) vs «Бюджет 150k» (daily/2026-08-05.md)"
    )
    _write_queue(
        vault,
        [
            {
                "kind": "conflict",
                "page": "compiled/decisions/budget.md",
                "summary": duplicate_summary,
                "since": DAY.isoformat(),
            },
            {
                "kind": "blocked-action",
                "page": "compiled/decisions/budget.md",
                "summary": "задача не создана: источник только forwarded",
                "since": DAY.isoformat(),
            },
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert digest.count(duplicate_summary) == 1
    assert "задача не создана: источник только forwarded" in digest


def test_decisions_queue_dedup_ignores_case_spacing_and_a_trailing_period(
    tmp_path, write_vault_manifest
):
    """The queue is written by the model, so a duplicate of a page conflict
    commonly differs from it only by case, an extra space, or a trailing
    period (``_normalize_decision_summary``). An exact string match would let
    both through and the owner would answer the same conflict twice."""
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
                DAY.isoformat(),
                "Бюджет 100k",
                "daily/2026-07-01.md",
                "Бюджет 150k",
                "daily/2026-08-05.md",
            )
        ],
    )
    conflict_summary = (
        "«Бюджет 100k» (daily/2026-07-01.md) vs «Бюджет 150k» (daily/2026-08-05.md)"
    )
    sloppy_duplicate = conflict_summary.upper().replace(" vs ", "  VS  ") + " ."
    _write_queue(
        vault,
        [
            {
                "kind": "conflict",
                "page": "compiled/decisions/budget.md",
                "summary": sloppy_duplicate,
                "since": DAY.isoformat(),
            }
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert conflict_summary in digest  # the page conflict itself still shows
    assert sloppy_duplicate not in digest


def test_decisions_queue_dedup_keeps_the_same_summary_on_another_page(
    tmp_path, write_vault_manifest
):
    """The de-dup key is the pair «страница + суть» (ТЗ), not the summary
    alone: two pages can legitimately raise word-for-word the same question,
    and folding them together would silently drop one page's decision."""
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
    )
    shared_summary = "источник не найден в vault"
    _write_queue(
        vault,
        [
            {
                "kind": "blocked-action",
                "page": "compiled/decisions/budget.md",
                "summary": shared_summary,
                "since": DAY.isoformat(),
            },
            {
                "kind": "blocked-action",
                "page": "compiled/decisions/hiring.md",
                "summary": shared_summary,
                "since": DAY.isoformat(),
            },
        ],
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert digest.count(shared_summary) == 2


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


def test_worth_a_look_picks_related_stale_page_only(tmp_path, write_vault_manifest):
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
    assert "**Стоит посмотреть**" in digest
    assert "compiled/topics/aurora-strategy.md" in digest
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
        "compiled/topics/warm.md", "Стратегия Аврора Warm", "topics", "warm",
        "2026-01-01",
    )
    cold = _revisit_candidate(
        "compiled/topics/cold.md", "Стратегия Аврора Cold", "topics", "cold",
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


def test_block_order_matches_tz_7_1(tmp_path, write_vault_manifest):
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
        conflicts_rows=[
            (
                DAY.isoformat(),
                "старое утверждение",
                "daily/2026-07-01.md",
                "новое утверждение",
                "daily/2026-08-05.md",
            )
        ],
    )
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

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    decisions_index = digest.index("**Требует решения**")
    changes_index = digest.index("**Что изменилось**")
    revisit_index = digest.index("**Стоит посмотреть**")
    assert decisions_index < changes_index < revisit_index


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


def test_corrupt_journal_forces_honest_digest_not_a_quiet_day(
    tmp_path, write_vault_manifest
):
    """End-to-end (corrupted journal on disk -> ``read_pass_status`` ->
    ``build_daily_digest``): on an otherwise quiet vault, a state file that
    exists but cannot be parsed must not silently produce the same digest
    as a genuinely quiet day -- the owner must see an honest line saying
    the last pass's state could not be read, not "Изменений и открытых
    вопросов нет.", and the digest must not be suppressed."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    journal = vault / ".session" / "compile-enrich.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{not valid json", encoding="utf-8")

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is not None
    assert (
        "Не удалось прочитать состояние прошлого прохода обогащения "
        "— статус неизвестен" in digest
    )
    assert "Изменений и открытых вопросов нет." not in digest


def test_failed_journal_with_missing_error_field_still_shows_fallback_line(
    tmp_path, write_vault_manifest
):
    """End-to-end (journal -> ``read_pass_status`` -> ``build_daily_digest``)
    version of the direct-``PassStatus`` fallback-line test above: a real
    ``"failed"`` journal with no ``error`` key must still surface the
    honest fallback line, not render like a quiet day."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_journal(vault, {"status": "failed"})

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is not None
    assert "Проход завершился с ошибкой, подробности недоступны" in digest


def test_read_pass_status_ok_and_failed_do_not_suppress_digest(
    tmp_path, write_vault_manifest
):
    """Verifies (by running, not by reasoning) that real journal statuses
    other than "no-work" ("ok", "failed") reach ``build_daily_digest`` and
    do not trip its ``status == "no-work"`` suppression check -- unlike
    ``test_read_pass_status_reads_real_no_work_journal`` above, which stops
    at the ``PassStatus`` returned by ``read_pass_status``, this drives the
    real value all the way into ``build_daily_digest``."""
    for journal_status, error in (("ok", ""), ("failed", "qmd unavailable")):
        vault = tmp_path / f"vault-{journal_status}"
        (vault / "compiled").mkdir(parents=True)
        write_vault_manifest(vault)
        _write_journal(vault, {"status": journal_status, "error": error})

        digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

        assert digest is not None


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


def test_budget_exhausted_forces_digest_on_otherwise_quiet_no_work_day(
    tmp_path, write_vault_manifest
):
    """ТЗ 5.5 инв 7: "При исчерпании бюджета проход завершается штатно,
    остаток остаётся в очереди, факт исчерпания попадает в дайджест.
    Молчаливое усечение запрещено." -- a "no-work" pass with no error and
    no decisions queued must still produce a digest when a budget was
    exhausted, instead of collapsing to the ``None`` a quiet day returns."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(status="no-work", budget_exhausted=("pages-per-pass",)),
    )

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "Бюджет прохода исчерпан" in digest


def test_budget_exhausted_line_translates_technical_name_not_shown_raw(
    tmp_path, write_vault_manifest
):
    """ТЗ: journal names like "pages-per-pass" are technical -- the owner
    must see a human sentence, not the code identifier verbatim."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(status="success", budget_exhausted=("pages-per-pass",)),
    )

    assert digest is not None
    assert "pages-per-pass" not in digest
    assert "лимит страниц" in digest


def test_no_budget_exhausted_adds_no_line(tmp_path, write_vault_manifest):
    """Non-regression: an ordinary pass with nothing exhausted must not gain
    a spurious "Бюджет" line."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "Бюджет" not in digest


def test_read_pass_status_no_work_with_budget_exhausted_does_not_suppress(
    tmp_path, write_vault_manifest
):
    """End-to-end (journal -> ``read_pass_status`` -> ``build_daily_digest``)
    version of ``test_budget_exhausted_forces_digest_on_otherwise_quiet_no_work_day``
    above, driven through the real journal reader instead of a hand-built
    ``PassStatus``."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_journal(
        vault,
        {"status": "no-work", "budget_exhausted": ["monthly-enrichments-per-page"]},
    )

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is not None
    assert "Бюджет прохода исчерпан" in digest


def test_budget_exhausted_labels_cover_every_name_the_core_can_write():
    """Задача N дефект 1: cross-check the full set of technical constraint
    names ``compiled_briefings.py`` (the core -- out of this task's edit
    zone, read-only) may actually record in the pass journal's
    ``budget_exhausted`` field against this module's translation table. If
    the core ever adds a new ``pass_obj.budget_exhausted.add("...")`` call
    site without a matching entry here, this test must fail instead of
    letting an untranslated technical name reach the owner silently via the
    unknown-name fallback."""
    import inspect
    import re

    from d_brain.services import compiled_briefings
    from d_brain.services.compiled_enrich_report import _BUDGET_EXHAUSTED_LABELS

    source = inspect.getsource(compiled_briefings)
    names = set(re.findall(r'budget_exhausted\.add\("([^"]+)"\)', source))

    assert names, "no budget_exhausted.add(...) call sites found in the core"
    assert names <= set(_BUDGET_EXHAUSTED_LABELS)


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


def test_queue_evictions_forces_digest_on_otherwise_quiet_no_work_day(
    tmp_path, write_vault_manifest
):
    """Mirrors ``test_budget_exhausted_forces_digest_on_otherwise_quiet_no_work_day``:
    a "no-work" pass with no error and no decisions queued must still
    produce a digest when the queue cap forced old entries out, instead of
    collapsing to the ``None`` a quiet day returns."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(status="no-work", queue_evictions=2),
    )

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "2" in digest
    assert "Очередь решений переполнена" in digest


def test_no_queue_evictions_adds_no_line(tmp_path, write_vault_manifest):
    """Non-regression: an ordinary pass with nothing evicted must not gain a
    spurious eviction line."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "переполнена" not in digest


def test_read_pass_status_no_work_with_queue_evictions_does_not_suppress(
    tmp_path, write_vault_manifest
):
    """End-to-end (journal -> ``read_pass_status`` -> ``build_daily_digest``),
    driven through the real journal reader instead of a hand-built
    ``PassStatus``."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_journal(vault, {"status": "no-work", "queue_evictions": 1})

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is not None
    assert "Очередь решений переполнена" in digest


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


def test_human_zone_ambiguous_pages_forces_digest_on_otherwise_quiet_no_work_day(
    tmp_path, write_vault_manifest
):
    """Same rationale as ``budget_exhausted``/``queue_evictions``: unlike
    those two, a page stuck on a broken marker does not even resolve itself
    next pass, so a "no-work" pass with no error and no decisions queued
    must still produce a digest instead of collapsing to ``None``."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(
            status="no-work",
            human_zone_ambiguous_pages=("compiled/projects/broken.md",),
        ),
    )

    assert digest is not None
    assert "**Требует решения**" in digest
    assert "[[compiled/projects/broken.md]]" in digest


def test_human_zone_ambiguous_pages_line_is_plain_and_names_the_page(
    tmp_path, write_vault_manifest
):
    """ТЗ: the owner is not a programmer, so the line must not read as a
    raw technical term -- and (unlike a budget name) the page path itself
    is the useful part, not just a count, since there is no "Очередь"
    screen listing these pages."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(
            status="success",
            human_zone_ambiguous_pages=("compiled/projects/broken.md",),
        ),
    )

    assert digest is not None
    assert "ambiguous" not in digest.lower()
    assert "[[compiled/projects/broken.md]]" in digest
    assert "human:start" in digest  # findable in the actual file
    assert "human:end" in digest


def test_human_zone_ambiguous_pages_line_caps_shown_pages_with_and_more_suffix(
    tmp_path, write_vault_manifest
):
    """Requirement: showing every single path forever would let one bad
    night dominate the whole digest -- past the cap, the rest are folded
    into an honest "и ещё N" instead of being shown or silently dropped."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    pages = tuple(f"compiled/projects/p{i}.md" for i in range(8))

    digest = build_daily_digest(
        vault,
        DAY,
        pass_status=PassStatus(status="success", human_zone_ambiguous_pages=pages),
    )

    assert digest is not None
    for page in pages[:5]:
        assert f"[[{page}]]" in digest
    for page in pages[5:]:
        assert f"[[{page}]]" not in digest
    assert "и ещё 3" in digest
    assert "8" in digest


def test_no_human_zone_ambiguous_pages_adds_no_line(tmp_path, write_vault_manifest):
    """Non-regression: with nothing broken, this line must not appear every
    day -- it would just become noise the owner learns to ignore."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="success"))

    assert digest is not None
    assert "human:start" not in digest
    assert "human-zone" not in digest.lower()


def test_read_pass_status_no_work_with_human_zone_ambiguous_pages_does_not_suppress(
    tmp_path, write_vault_manifest
):
    """End-to-end (journal -> ``read_pass_status`` -> ``build_daily_digest``):
    a page with a broken marker, otherwise nothing else happening that day,
    still reaches the owner with an actionable line instead of "Изменений и
    открытых вопросов нет"."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_journal(
        vault,
        {
            "status": "no-work",
            "human_zone_ambiguous_pages": ["compiled/projects/broken.md"],
        },
    )

    digest = build_daily_digest(vault, DAY, pass_status=read_pass_status(vault))

    assert digest is not None
    assert "[[compiled/projects/broken.md]]" in digest
    assert "Изменений и открытых вопросов нет" not in digest


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


def test_collect_weekly_review_window_and_review_pick(
    tmp_path, write_vault_manifest
):
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
    monkeypatch.setattr(
        "sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"]
    )

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "compiled/topics/aurora.md" in out
    assert not (vault / "summaries").exists()


def test_dry_run_quiet_day_prints_fallback_line(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    """``read_pass_status`` falls back to ``PassStatus(status="success")``
    when no pass journal exists (as in this fresh temp vault), so an empty
    vault renders the digest's own "nothing happened" fallback line rather
    than hitting ``build_daily_digest``'s "no-work" suppression -- that
    suppression only fires for a real journal whose status is literally
    "no-work"."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        "sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"]
    )

    exit_code = run_compiled_digest.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Изменений и открытых вопросов нет." in out


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
    monkeypatch.setattr(
        "sys.argv", ["prog", "--date", DAY.isoformat(), "--dry-run"]
    )

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


def test_fact_check_queue_evictions_reach_the_digest(tmp_path, write_vault_manifest):
    """Code review: the monthly fact-check pass runs with no active
    ``CompileEnrichPass``, so it journals its evictions separately -- and
    nothing read that journal, so ТЗ 7.2's "факт вытеснения попадает в
    дайджест" silently did not hold for the pass that most often causes it."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_fact_check_journal(vault, day=DAY.isoformat(), evictions=7)

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "Очередь решений переполнена: 7 старых пункт(ов)" in digest


def test_fact_check_queue_evictions_add_to_the_pass_own_count(
    tmp_path, write_vault_manifest
):
    """Both passes can evict on the same day; the digest shows one honest
    total, not whichever number it happened to read first."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_fact_check_journal(vault, day=DAY.isoformat(), evictions=2)

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="ok", queue_evictions=3)
    )

    assert digest is not None
    assert "Очередь решений переполнена: 5 старых пункт(ов)" in digest


def test_fact_check_journal_from_another_day_is_ignored(
    tmp_path, write_vault_manifest
):
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


def test_broken_fact_check_journal_never_breaks_the_digest(
    tmp_path, write_vault_manifest
):
    """Fail-safe like ``_read_decisions_queue``: a corrupt journal must cost
    the owner the eviction line, not the whole digest."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-fact-check.json").write_text("{not json", encoding="utf-8")

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="ok", queue_evictions=1)
    )

    assert digest is not None
    assert "Очередь решений переполнена: 1 старых пункт(ов)" in digest


def test_eviction_line_quotes_the_real_queue_cap(tmp_path, write_vault_manifest):
    """The cap belongs to ``decisions_queue.QUEUE_CAP``; the digest used to
    spell it out as a literal, which goes stale the moment the cap moves."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="ok", queue_evictions=1)
    )

    assert digest is not None
    assert f"лимит очереди {QUEUE_CAP} пунктов" in digest


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


def test_a_crashed_background_drain_reaches_the_owner(tmp_path, write_vault_manifest):
    """The drain is spawned detached with stdout/stderr on DEVNULL, so its
    crash journal was the only record it had died -- and nothing read that
    journal. A dead drain stops enriching pages during the day while looking
    exactly like a quiet day."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue_worker_crash_journal(
        vault, crashed_at=f"{DAY.isoformat()}T14:05:11+03:00"
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "Фоновая обработка очереди страниц аварийно остановилась" in digest
    assert "qmd index is locked" in digest


def test_a_crash_journal_without_a_reason_is_still_reported(
    tmp_path, write_vault_manifest
):
    """A crash journal that lost its ``error`` block still proves the drain
    died -- report it rather than dropping the line for want of a reason."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_queue_worker_crash_journal(
        vault, crashed_at=f"{DAY.isoformat()}T14:05:11+03:00", message=None
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "причина не записана" in digest


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


def test_a_broken_crash_journal_never_breaks_the_digest(
    tmp_path, write_vault_manifest
):
    """Fail-safe like every other journal this module folds in."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-queue-worker.json").write_text("{not json", encoding="utf-8")

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="ok", queue_evictions=1)
    )

    assert digest is not None
    assert "Очередь решений переполнена: 1 старых пункт(ов)" in digest


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


def test_a_source_the_queue_gave_up_on_reaches_the_owner(
    tmp_path, write_vault_manifest
):
    """The owner wrote a note, three compile attempts failed, and the queue
    entry -- the only trigger that would ever compile it -- was deleted. The
    drain that produces almost all of these runs detached with stdout/stderr
    on DEVNULL, so its returned ``errors`` list went nowhere: the note simply
    never appeared in ``compiled/`` and nothing said why."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_dropped_sources_journal(vault, "daily/2026-08-05.md")

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "Не удалось собрать страницы по 1 источник(ам)" in digest
    assert "[[daily/2026-08-05.md]]" in digest


def test_dropped_sources_are_not_expired_by_the_calendar(
    tmp_path, write_vault_manifest
):
    """Unlike the crash and fact-check journals, this one lists what is
    *still* missing rather than what happened on a date -- the writer clears
    an entry itself once that source finally compiles. A source dropped last
    week and still uncompiled is still the owner's to act on."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-dropped-sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_path": "daily/2026-07-20.md",
                        "dropped_at": f"{(DAY - timedelta(days=16)).isoformat()}"
                        "T14:05:11+03:00",
                        "attempts": 3,
                        "errors": ["backend-refused"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "[[daily/2026-07-20.md]]" in digest


def test_a_backend_outage_dropping_many_sources_does_not_bury_the_digest(
    tmp_path, write_vault_manifest
):
    """One bad night for the model backend can drop a whole batch at once;
    the line names a capped sample and counts the rest, same as the
    human-zone line."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    _write_dropped_sources_journal(
        vault, *(f"daily/2026-07-{day:02d}.md" for day in range(1, 9))
    )

    digest = build_daily_digest(vault, DAY, pass_status=PassStatus(status="no-work"))

    assert digest is not None
    assert "Не удалось собрать страницы по 8 источник(ам)" in digest
    assert "и ещё 3" in digest
    assert "[[daily/2026-07-06.md]]" not in digest


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


def test_a_broken_dropped_sources_journal_never_breaks_the_digest(
    tmp_path, write_vault_manifest
):
    """Fail-safe like every other journal this module folds in."""
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)
    session = vault / ".session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "compile-dropped-sources.json").write_text(
        "{not json", encoding="utf-8"
    )

    digest = build_daily_digest(
        vault, DAY, pass_status=PassStatus(status="ok", queue_evictions=1)
    )

    assert digest is not None
    assert "Очередь решений переполнена: 1 старых пункт(ов)" in digest
