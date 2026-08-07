"""Tests for owner briefs assembled from compiled/ pages (ТЗ 7.6).

``build_brief`` is pure and read-only (see module docstring), so most tests
here only assemble a temporary ``compiled/**`` tree -- no vault write path is
exercised. CLI tests for ``run_compiled_brief.main`` monkeypatch
``write_validated_vault_markdown``/``send_telegram_text_sync``/``QmdService``
instead of calling them for real, mirroring
``tests/test_compiled_enrich_report.py``: in this sandbox the real
``write_validated_vault_markdown`` fails with ``UnsafeVaultPathError`` for
environment reasons (missing kernel privilege), not because of anything in
this task's code -- see the final report. ``QmdService`` is mocked purely to
keep the suite from shelling out to ``uv run ... memory-engine.py`` on every
CLI test (mirrors ``tests/test_qmd_service.py``'s ``run_qmd`` tests).
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain import run_compiled_brief
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefs import BriefResult, build_brief
from d_brain.services.frontmatter import parse_frontmatter_bytes, validate_document

DAY = date(2026, 8, 5)


def _brief_section(markdown: str, heading: str) -> list[str]:
    """The lines under one ``**Heading**`` of a rendered brief.

    A plain ``"value" in markdown`` assert passes no matter which section the
    value ended up under, so it cannot tell a correctly assembled brief from
    one whose blocks were swapped -- the rationale showing up under "Когда
    принято" would still read as green. Sections run from their bold heading
    to the next one (or the end).
    """
    lines = markdown.split("\n")
    start = lines.index(f"**{heading}**")
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("**") and line.endswith("**"):
            break
        if line:
            section.append(line)
    return section


def _compiled_page_text(
    *,
    domain: str,
    title: str,
    description: str = "",
    tier: str = "active",
    sources_trust: str | None = None,
    decision_date: str = "",
    supersedes: list[str] | None = None,
    rationale: str = "",
    alternatives: list[str] | None = None,
    current_state: str = "",
    recent_changes: list[str] | None = None,
    open_loops: list[str] | None = None,
    key_decisions: list[str] | None = None,
    sources_rows: list[tuple[str, str, str]] | None = None,
    claim_history_rows: list[tuple[str, str, str, str]] | None = None,
    conflicts_rows: list[tuple[str, str, str, str, str]] | None = None,
    source_paths: list[str] | None = None,
) -> str:
    """Render one ``compiled/**`` page in exactly the shape
    ``CompiledBriefingService`` itself writes and parses, so the real
    parsers this task reuses (``_frontmatter_fields``/``_section_text``/
    ``_section_bullets``/``_sources_shaped_rows``/``_claim_history_rows``/
    ``_open_conflicts_rows``) exercise real regexes instead of a hand-rolled
    shortcut (mirrors ``tests/test_compiled_enrich_report.py``).
    """
    frontmatter = [
        "---",
        "type: compiled-briefing",
        f"domain: {domain}",
        f'description: "{description or title}"',
        "status: active",
        f"created: {DAY.isoformat()}",
        f"updated: {DAY.isoformat()}",
        "freshness_state: fresh",
        "confidence: high",
        f"last_accessed: {DAY.isoformat()}",
        "relevance: 0.80",
        f"tier: {tier}",
    ]
    if sources_trust is not None:
        frontmatter.append(f"sources_trust: {sources_trust}")
    if domain == "decisions":
        frontmatter.append("record_kind: decision")
        frontmatter.append(f"decision_date: {decision_date}")
        # ТЗ 5.3 schema: "supersedes": ["decision identifier", ...] -- free
        # model text, written via json.dumps in production
        # (CompiledBriefingService._render_briefing); mirrored exactly here.
        frontmatter.append(
            f"supersedes: {json.dumps(supersedes or [], ensure_ascii=False)}"
        )
    frontmatter.append("---")
    frontmatter.append("")

    body = [f"# {title}", ""]
    if domain == "decisions":
        body += ["## Rationale", rationale or "(no rationale)", ""]
        body += ["## Alternatives Considered"]
        body += [f"- {item}" for item in (alternatives or [])] or ["- (none)"]
        body += [""]
    body += ["## Current State", current_state or "(none)", ""]
    body += ["## Recent Changes"]
    body += [f"- {item}" for item in (recent_changes or [])] or ["- (none)"]
    body += [""]
    body += ["## Open Loops"]
    body += [f"- {item}" for item in (open_loops or [])] or ["- (none)"]
    body += [""]
    body += ["## Key Decisions"]
    body += [f"- {item}" for item in (key_decisions or [])] or ["- (none)"]
    body += [""]
    body += ["## Sources"]
    body += [f"- [[{path}]]" for path in (source_paths or [])] or ["- (none)"]
    body += [""]
    body += ["## Sources That Shaped This Page", ""]
    body += ["| Date | Source | What Added |", "| --- | --- | --- |"]
    body += [
        f"| {row_date} | [[{source}]] | {what} |"
        for row_date, source, what in (sources_rows or [])
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
    return "\n".join(frontmatter) + "\n".join(body) + "\n"


def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compiled_page_text(**kwargs), encoding="utf-8")  # type: ignore[arg-type]


# --- target resolution (ТЗ 7.6 step 1) -----------------------------------


def test_exact_slug_match_wins_over_ranking(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/acme-renewal.md",
        domain="decisions",
        title="Продление контракта Acme",
        description="Продлить контракт с Acme на год",
        decision_date="2026-07-01",
        rationale="Клиент доволен, цена устраивает обе стороны.",
    )
    # A decoy that would out-rank the target on free-text terms alone.
    _write_page(
        vault,
        "compiled/decisions/acme-renewal-2025.md",
        domain="decisions",
        title="Продление контракта Acme продление продление",
        description="Продление продление продление",
        decision_date="2025-07-01",
    )

    result = build_brief(vault, brief_type="decision", query="acme-renewal")

    assert result is not None
    assert result.source_rel_path == "compiled/decisions/acme-renewal.md"


def test_exact_path_match(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
    )

    result = build_brief(
        vault, brief_type="topic", query="compiled/topics/quantum-widgets.md"
    )

    assert result is not None
    assert result.source_rel_path == "compiled/topics/quantum-widgets.md"


def test_fallback_ranking_restricted_to_domain(tmp_path, write_vault_manifest):
    """A wrong-domain page must not win a ``project`` brief even when it
    out-scores the real target on ``_rank_candidates``'s raw score.

    Code-review regression: the previous fixture's decoy lost on raw score
    even with the domain filter removed, so the test stayed green after the
    filter itself was deleted. Here the decoy's title is a near-verbatim,
    much longer match for the query text than the real target's title, so
    it wins the *unfiltered* ranking outright (verified by temporarily
    dropping the domain check in ``_find_target`` while writing this test --
    see the task report) -- only the domain restriction saves this brief.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    query = "Расскажи про статус проекта Borealis по клиенту Contoso"
    _write_page(
        vault,
        "compiled/projects/borealis.md",
        domain="projects",
        title="Проект Borealis",
        description="Проект Borealis для клиента Contoso",
        current_state="Проект идёт по плану.",
    )
    # Wrong domain (topics), but its title is almost the whole query
    # verbatim -- a big "title in query" bonus plus heavy token overlap in
    # ``_rank_candidates``, enough to outrank the actual project page.
    _write_page(
        vault,
        "compiled/topics/borealis-status.md",
        domain="topics",
        title="Статус проекта Borealis по клиенту Contoso",
        description="Обсуждение статуса Borealis",
        current_state="Статус обсуждается регулярно.",
    )

    result = build_brief(vault, brief_type="project", query=query)

    assert result is not None
    assert result.domain == "projects"
    assert result.source_rel_path == "compiled/projects/borealis.md"


def test_strong_page_in_another_domain_does_not_hide_the_real_target(
    tmp_path, write_vault_manifest
):
    """The ranker's ``min_score`` cut is relative to the best page it saw,
    so ranking the whole vault and filtering by domain afterwards let a
    strong page in another domain raise the bar above the requested
    domain's own page -- and the brief then reported "не нашёл" for a page
    sitting right there on disk. Restricting the field before scoring is
    what keeps the weaker but genuinely relevant page reachable."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/миграция.md",
        domain="topics",
        title="миграция",
        description="общая тема про миграцию",
        current_state="Идёт.",
    )
    _write_page(
        vault,
        "compiled/projects/klient-migraciya.md",
        domain="projects",
        title="миграция данных клиента",
        description="проект: миграция биллинга клиента",
        current_state="Активен.",
    )

    result = build_brief(vault, brief_type="project", query="миграция")

    assert result is not None
    assert result.source_rel_path == "compiled/projects/klient-migraciya.md"


def test_no_match_returns_none_not_exception(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
    )

    result = build_brief(vault, brief_type="decision", query="совершенно другое")

    assert result is None


def test_missing_compiled_directory_returns_none(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    vault.mkdir()
    write_vault_manifest(vault)

    result = build_brief(vault, brief_type="topic", query="anything")

    assert result is None


def test_unknown_brief_type_raises(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)

    with pytest.raises(ValueError):
        build_brief(vault, brief_type="bogus", query="x")


def test_broken_page_frontmatter_does_not_crash_lookup(tmp_path, write_vault_manifest):
    """One malformed page (missing closing ``---``) in the target's own
    domain must not take down the whole lookup -- mirrors
    ``CompiledBriefingService._iter_candidates``/``_frontmatter_fields``
    tolerance already exercised by ``test_compiled_enrich_report.py``."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/acme-renewal.md",
        domain="decisions",
        title="Продление контракта Acme",
        decision_date="2026-07-01",
    )
    broken_path = vault / "compiled" / "decisions" / "broken.md"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text(
        "---\ntype: compiled-briefing\ndomain: decisions\n# Сломанная страница\n",
        encoding="utf-8",
    )

    result = build_brief(vault, brief_type="decision", query="acme-renewal")

    assert result is not None
    assert result.source_rel_path == "compiled/decisions/acme-renewal.md"


# --- decision brief content -----------------------------------------------


def test_decision_brief_contents(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/acme-renewal.md",
        domain="decisions",
        title="Продление контракта Acme",
        description="Продлить контракт с Acme на год",
        sources_trust="own",
        decision_date="2026-07-01",
        rationale="Клиент доволен, цена устраивает обе стороны.",
        alternatives=["Разорвать контракт", "Пересмотреть цену"],
        sources_rows=[
            (DAY.isoformat(), "daily/2026-08-05.md", "зафиксировано продление")
        ],
    )

    result = build_brief(vault, brief_type="decision", query="acme-renewal")

    assert result is not None
    markdown = result.markdown
    assert "**Бриф по решению — Продление контракта Acme**" in markdown
    # Each value under its own heading, not merely somewhere in the brief.
    assert _brief_section(markdown, "В чём суть") == [
        "Продлить контракт с Acme на год"
    ]
    assert _brief_section(markdown, "Когда принято") == ["2026-07-01"]
    assert _brief_section(markdown, "Обоснование") == [
        "Клиент доволен, цена устраивает обе стороны."
    ]
    assert _brief_section(markdown, "Отклонённые варианты") == [
        "- Разорвать контракт",
        "- Пересмотреть цену",
    ]
    assert _brief_section(markdown, "Что это решение отменяет") == [
        "- не зафиксировано"
    ]
    assert _brief_section(markdown, "Источники") == [
        "- 2026-08-05 · [[daily/2026-08-05.md]] — зафиксировано продление"
    ]


def test_decision_brief_supersedes_reads_own_frontmatter_field(
    tmp_path, write_vault_manifest
):
    """ТЗ 7.6: "что это решение отменяет".

    Regression for a code-review defect: the section used to reverse-scan
    every page's "Claim History" table for a "Superseded By" cell equal to
    this page's own path -- but production code
    (``CompiledBriefingService._apply_claims_and_conflicts``) always writes
    the *source* that triggered a supersession there, never a compiled
    page's path, so that cell can never match and the section was always
    empty (confirmed by rendering a page through the real
    ``_render_briefing`` -- see the task report).

    The actual answer lives on the decision page's own ``supersedes``
    frontmatter field. It is free-form model text ("decision identifier"
    per the ТЗ 5.3 schema), not a vault path, so it must render as plain
    text with an explicit note -- never as a ``[[wikilink]]``, which would
    imply a real page that may not exist.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/acme-renewal.md",
        domain="decisions",
        title="Продление контракта Acme",
        decision_date="2026-07-01",
        supersedes=["Продление 2025 года", "Скидка для Acme"],
    )
    # A claim-history row on another page whose "Superseded By" cell happens
    # to hold this decision's path -- production code never writes this,
    # but if some other page's row does, it must NOT leak into this
    # section (that was the old, broken behavior).
    _write_page(
        vault,
        "compiled/topics/acme-pricing.md",
        domain="topics",
        title="Ценообразование Acme",
        claim_history_rows=[
            (
                "2026-06-01",
                "daily/2026-06-01.md",
                "цена фиксирована до конца года",
                "compiled/decisions/acme-renewal.md",
            )
        ],
    )

    result = build_brief(vault, brief_type="decision", query="acme-renewal")

    assert result is not None
    markdown = result.markdown
    assert "Продление 2025 года" in markdown
    assert "Скидка для Acme" in markdown
    assert "не ссылка на страницу" in markdown
    # Free model text, never rendered as a page link.
    assert "[[Продление 2025 года]]" not in markdown
    assert "[[Скидка для Acme]]" not in markdown
    # The unrelated page's claim-history row must not leak in either.
    assert "цена фиксирована до конца года" not in markdown


def test_decision_brief_missing_sections_show_placeholders(
    tmp_path, write_vault_manifest
):
    """ТЗ resilience: a page missing Rationale/Alternatives/decision_date
    must not crash and must not silently show nothing."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    path = vault / "compiled" / "decisions" / "bare.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: compiled-briefing\n"
        "domain: decisions\n"
        f'description: ""\n'
        f"last_accessed: {DAY.isoformat()}\n"
        "relevance: 0.5\n"
        "tier: active\n"
        "---\n\n"
        "# Голое решение\n",
        encoding="utf-8",
    )

    result = build_brief(vault, brief_type="decision", query="bare")

    assert result is not None
    markdown = result.markdown
    assert "не зафиксировано" in markdown
    assert "не зафиксированы" in markdown
    assert "источники не зафиксированы" in markdown


# --- trust callout (ТЗ 4.4) -----------------------------------------------


@pytest.mark.parametrize("trust", ["forwarded", "inferred"])
def test_low_trust_gets_explicit_callout_near_top(
    tmp_path, write_vault_manifest, trust
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        sources_trust=trust,
        current_state="Пересланное сообщение без проверки.",
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    markdown = result.markdown
    assert "Внимание" in markdown
    assert trust in markdown
    # Not buried at the bottom: the callout is the second line, right under
    # the title, well before the "Источники" section.
    assert markdown.index("Внимание") < markdown.index("**Источники**")
    assert markdown.splitlines()[1].startswith("**Внимание")


@pytest.mark.parametrize("trust", ["own", "integration"])
def test_high_trust_no_callout(tmp_path, write_vault_manifest, trust):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        sources_trust=trust,
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    assert "Внимание" not in result.markdown
    assert trust in result.markdown


def test_missing_trust_field_does_not_crash(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    assert "не определён" in result.markdown


# --- topic/project brief content ------------------------------------------


def test_topic_brief_contents(tmp_path, write_vault_manifest):
    """ТЗ 7.6: "текущее состояние, ключевые решения, открытые вопросы,
    участники" -- ключевые решения come from the page's own "## Key
    Decisions" section, not from "Recent Changes" (that section is
    project-only, see ``test_project_brief_contents``)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="Исследование продолжается, прототип готов.",
        key_decisions=["Выбрать поставщика B", "Отложить запуск на месяц"],
        open_loops=["Нужно согласовать бюджет"],
        conflicts_rows=[
            (
                "2026-08-01",
                "поставщик A дешевле",
                "daily/2026-07-01.md",
                "поставщик B дешевле",
                "daily/2026-08-01.md",
            )
        ],
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    markdown = result.markdown
    assert "**Бриф по теме — Квантовые виджеты**" in markdown
    assert _brief_section(markdown, "Текущее состояние") == [
        "Исследование продолжается, прототип готов."
    ]
    assert _brief_section(markdown, "Ключевые решения") == [
        "- Выбрать поставщика B",
        "- Отложить запуск на месяц",
    ]
    assert _brief_section(markdown, "Открытые вопросы") == [
        "- Нужно согласовать бюджет"
    ]
    assert _brief_section(markdown, "Открытые конфликты") == [
        "- 2026-08-01: «поставщик A дешевле» (источник [[daily/2026-07-01.md]]) "
        "vs «поставщик B дешевле» (источник [[daily/2026-08-01.md]])"
    ]
    assert _brief_section(markdown, "Источники") == [
        "- 2026-08-05 · [[daily/2026-08-05.md]] — обновление"
    ]
    # ТЗ 7.6 lists "недавние изменения" for the project brief only.
    assert "**Последние изменения**" not in markdown
    assert "**Недавние изменения**" not in markdown


def test_project_brief_contents(tmp_path, write_vault_manifest):
    """ТЗ 7.6: "состояние, недавние изменения, обязательства, риски" --
    недавние изменения come from "## Recent Changes"; "открытые вопросы"/
    "ключевые решения" are topic-only (see ``test_topic_brief_contents``)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/projects/borealis.md",
        domain="projects",
        title="Проект Borealis",
        current_state="Идёт по плану.",
        recent_changes=["Подписан акт", "Сдвинут дедлайн на неделю"],
        open_loops=["Нужно согласовать бюджет"],
        key_decisions=["Не должно попасть в бриф проекта"],
        conflicts_rows=[
            (
                "2026-08-01",
                "срок сдвинут на неделю",
                "daily/2026-07-01.md",
                "срок сдвинут на две недели",
                "daily/2026-08-01.md",
            )
        ],
        sources_rows=[(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    )

    result = build_brief(vault, brief_type="project", query="borealis")

    assert result is not None
    assert result.domain == "projects"
    markdown = result.markdown
    assert "**Бриф по проекту — Проект Borealis**" in markdown
    assert _brief_section(markdown, "Состояние") == ["Идёт по плану."]
    assert _brief_section(markdown, "Недавние изменения") == [
        "- Подписан акт",
        "- Сдвинут дедлайн на неделю",
    ]
    assert _brief_section(markdown, "Открытые конфликты") == [
        "- 2026-08-01: «срок сдвинут на неделю» (источник [[daily/2026-07-01.md]]) "
        "vs «срок сдвинут на две недели» (источник [[daily/2026-08-01.md]])"
    ]
    assert _brief_section(markdown, "Источники") == [
        "- 2026-08-05 · [[daily/2026-08-05.md]] — обновление"
    ]
    # ТЗ 7.6 lists "открытые вопросы"/"ключевые решения" for the topic
    # brief only -- must not leak into a project brief.
    assert "**Открытые вопросы**" not in markdown
    assert "**Ключевые решения**" not in markdown
    assert "Не должно попасть в бриф проекта" not in markdown


def test_topic_and_project_briefs_render_different_sections(
    tmp_path, write_vault_manifest
):
    """Regression for the original defect: ``_topic_or_project_brief``
    rendered the exact same section headings for both brief types (only the
    title/label differed). Built from one page with every field populated so
    a leftover shared renderer would make both heading sets equal again."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    common = {
        "current_state": "В процессе.",
        "recent_changes": ["Изменение A"],
        "open_loops": ["Вопрос A"],
        "key_decisions": ["Решение A"],
        "sources_rows": [(DAY.isoformat(), "daily/2026-08-05.md", "обновление")],
    }
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        **common,
    )
    _write_page(
        vault,
        "compiled/projects/borealis.md",
        domain="projects",
        title="Проект Borealis",
        **common,
    )

    topic = build_brief(vault, brief_type="topic", query="quantum-widgets")
    project = build_brief(vault, brief_type="project", query="borealis")

    assert topic is not None
    assert project is not None
    # Assert the ТЗ 7.6 split by name, not by "the heading sets differ":
    # the state heading already differs in wording between the two types, so
    # a set comparison stays green even when one renderer leaks the other's
    # body (found in review).
    assert "**Ключевые решения**" in topic.markdown
    assert "**Недавние изменения**" not in topic.markdown
    assert "**Недавние изменения**" in project.markdown
    assert "**Ключевые решения**" not in project.markdown


def test_topic_brief_never_renders_participants_section(tmp_path, write_vault_manifest):
    """ТЗ 7.6 lists "участники" for the topic brief, but no compiled page
    field carries who is involved (checked ``COMPILE_JSON_EXAMPLE`` and
    ``page-schema.md`` -- see ``_topic_brief``'s docstring). The section
    must be entirely absent, not an empty placeholder claiming the page was
    checked."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    assert "Участники" not in result.markdown


def test_project_brief_never_renders_commitments_or_risks_sections(
    tmp_path, write_vault_manifest
):
    """ТЗ 7.6 lists "обязательства"/"риски" for the project brief, but no
    compiled page field survives to carry either (see ``_project_brief``'s
    docstring for exactly where this was checked). Both sections must be
    entirely absent, not empty placeholders."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/projects/borealis.md",
        domain="projects",
        title="Проект Borealis",
        current_state="Идёт по плану.",
    )

    result = build_brief(vault, brief_type="project", query="borealis")

    assert result is not None
    markdown = result.markdown
    assert "Обязательства" not in markdown
    assert "Риски" not in markdown


def test_topic_brief_missing_sections_show_placeholders(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    path = vault / "compiled" / "topics" / "bare.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: compiled-briefing\n"
        "domain: topics\n"
        f'description: ""\n'
        f"last_accessed: {DAY.isoformat()}\n"
        "relevance: 0.5\n"
        "tier: active\n"
        "---\n\n"
        "# Голая тема\n",
        encoding="utf-8",
    )

    result = build_brief(vault, brief_type="topic", query="bare")

    assert result is not None
    markdown = result.markdown
    assert "не зафиксировано" in markdown
    assert "- нет" in markdown
    assert "открытых конфликтов нет" in markdown
    assert "источники не зафиксированы" in markdown


def test_sources_fallback_to_plain_sources_section_when_table_empty(
    tmp_path, write_vault_manifest
):
    """ТЗ 7.6 provenance (``compiled_briefs._sources_block``): a page
    compiled before the "Sources That Shaped This Page" table existed only
    carries the older "## Sources" link list -- this is the main path for
    every page compiled that far back, and must still surface its sources
    rather than falling through to the "not recorded" placeholder.

    Code-review regression: this fallback branch was deleted and all 24
    tests in the suite stayed green, because none of them exercised a page
    with an empty shaped-sources table but a non-empty "## Sources" section
    -- confirmed by removing the branch locally and re-running this test,
    which goes red (see the task report).
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
        source_paths=["daily/2026-08-04.md", "thoughts/idea.md"],
        # sources_rows intentionally omitted: the shaped table renders with
        # no data rows, mirroring a page compiled before that table existed.
    )

    result = build_brief(vault, brief_type="topic", query="quantum-widgets")

    assert result is not None
    markdown = result.markdown
    assert "[[daily/2026-08-04.md]]" in markdown
    assert "[[thoughts/idea.md]]" in markdown
    assert "источники не зафиксированы" not in markdown


# --- run_compiled_brief CLI -----------------------------------------------


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_brief, "get_settings", lambda: SimpleNamespace(vault_path=vault)
    )


def test_render_note_passes_write_time_frontmatter_validation(
    tmp_path, write_vault_manifest
):
    """The write path (``write_validated_vault_markdown``) is fully mocked
    in the CLI tests below because the real one cannot run in this sandbox
    (see module docstring) -- so run the bytes ``_render_note`` builds
    through the same validation the write path applies
    (``parse_frontmatter_bytes`` + ``validate_document``) without touching
    disk."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    manifest = load_manifest_for_vault(vault)
    result = BriefResult(
        brief_type="decision",
        domain="decisions",
        slug="acme-renewal",
        source_rel_path="compiled/decisions/acme-renewal.md",
        title="Продление контракта Acme",
        markdown="**Бриф по решению — Продление контракта Acme**\n\nтест",
    )

    content = run_compiled_brief._render_note(result, DAY)
    document = parse_frontmatter_bytes(content)
    relative_path = "summaries/briefs/2026-08-05-decision-acme-renewal.md"

    _route, missing, invalid = validate_document(relative_path, document, manifest)

    assert missing == ()
    assert invalid == ()


def test_dry_run_prints_brief_without_writing_or_sending(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
    )
    _patch_settings(monkeypatch, vault)

    def _fail_write(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not write")

    def _fail_send(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not send")

    def _fail_qmd(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not touch the source page")

    monkeypatch.setattr(
        run_compiled_brief, "write_validated_vault_markdown", _fail_write
    )
    monkeypatch.setattr(run_compiled_brief, "send_telegram_text_sync", _fail_send)
    monkeypatch.setattr(run_compiled_brief, "QmdService", _fail_qmd)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--type", "topic", "--query", "quantum-widgets", "--dry-run"],
    )

    exit_code = run_compiled_brief.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Квантовые виджеты" in out
    assert "В разработке." in out
    assert not (vault / "summaries").exists()


def test_dry_run_topic_and_project_briefs_differ_via_cli(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    """Code-review defect: topic and project briefs used to render the same
    section shape end to end (only the title differed). Exercised through
    ``run_compiled_brief.main()`` itself -- the real entry point -- rather
    than ``build_brief``, so this also covers argument parsing and stdout
    formatting, not just ``compiled_briefs`` internals."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
        recent_changes=["Изменение A"],
        open_loops=["Вопрос A"],
        key_decisions=["Решение A"],
    )
    _write_page(
        vault,
        "compiled/projects/borealis.md",
        domain="projects",
        title="Проект Borealis",
        current_state="В разработке.",
        recent_changes=["Изменение A"],
        open_loops=["Вопрос A"],
        key_decisions=["Решение A"],
    )
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--type", "topic", "--query", "quantum-widgets", "--dry-run"],
    )
    assert run_compiled_brief.main() == 0
    topic_out = capsys.readouterr().out

    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--type", "project", "--query", "borealis", "--dry-run"],
    )
    assert run_compiled_brief.main() == 0
    project_out = capsys.readouterr().out

    topic_headings = {
        line for line in topic_out.splitlines() if line.startswith("**")
    }
    project_headings = {
        line for line in project_out.splitlines() if line.startswith("**")
    }
    topic_headings.discard("**Бриф по теме — Квантовые виджеты**")
    project_headings.discard("**Бриф по проекту — Проект Borealis**")
    assert topic_headings != project_headings
    assert "**Ключевые решения**" in topic_out
    assert "**Недавние изменения**" in project_out
    assert "**Ключевые решения**" not in project_out
    assert "**Открытые вопросы**" not in project_out


def test_dry_run_not_found_prints_clear_message(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--type", "topic", "--query", "nothing-here", "--dry-run"],
    )

    exit_code = run_compiled_brief.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "not-found" in out
    assert not (vault / "summaries").exists()


def test_main_writes_brief_file_and_sends_telegram(
    tmp_path, write_vault_manifest, monkeypatch
):
    # ``run_compiled_brief.py`` has no ``--date`` flag (unlike
    # ``run_compiled_digest.py``): the task spec lists only
    # --type/--query/--dry-run, so the written path is keyed off the real
    # wall-clock date -- assert against that instead of a fixed literal.
    today = date.today()
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
        current_state="В разработке.",
    )
    _patch_settings(monkeypatch, vault)

    write_calls: list[tuple[Path, bytes, dict]] = []
    send_calls: list[tuple[str, dict]] = []
    touch_calls: list[list[str]] = []

    def _fake_write(vault_path, path, content, **kwargs):  # type: ignore[no-untyped-def]
        del vault_path
        write_calls.append((path, content, kwargs))

    def _fake_send(text, **kwargs):  # type: ignore[no-untyped-def]
        send_calls.append((text, kwargs))

    class _FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == vault

        def touch_notes(self, targets: list[str]) -> None:
            touch_calls.append(list(targets))

    monkeypatch.setattr(
        run_compiled_brief, "write_validated_vault_markdown", _fake_write
    )
    monkeypatch.setattr(run_compiled_brief, "send_telegram_text_sync", _fake_send)
    monkeypatch.setattr(run_compiled_brief, "QmdService", _FakeQmdService)
    monkeypatch.setattr(
        "sys.argv", ["prog", "--type", "topic", "--query", "quantum-widgets"]
    )

    exit_code = run_compiled_brief.main()

    assert exit_code == 0
    assert len(write_calls) == 1
    path, content, kwargs = write_calls[0]
    assert path == (
        vault / "summaries" / "briefs" / f"{today.isoformat()}-topic-quantum-widgets.md"
    )
    assert b"type: compiled-brief" in content
    assert "Квантовые виджеты".encode() in content
    assert kwargs.get("require_absent") is not True
    assert len(send_calls) == 1
    text, send_kwargs = send_calls[0]
    assert "Квантовые виджеты" in text
    assert send_kwargs.get("rich") is True
    # ТЗ 6.2: the page that reached this brief gets touched.
    assert touch_calls == [["compiled/topics/quantum-widgets.md"]]


def test_telegram_send_failure_is_logged_not_raised(
    tmp_path, write_vault_manifest, monkeypatch, caplog
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
    )
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_brief, "write_validated_vault_markdown", lambda *a, **k: None
    )

    class _NoopQmdService:
        def __init__(self, vault_path: Path) -> None:
            del vault_path

        def touch_notes(self, targets: list[str]) -> None:
            del targets

    monkeypatch.setattr(run_compiled_brief, "QmdService", _NoopQmdService)

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(run_compiled_brief, "send_telegram_text_sync", _boom)
    monkeypatch.setattr(
        "sys.argv", ["prog", "--type", "topic", "--query", "quantum-widgets"]
    )

    with caplog.at_level(logging.WARNING):
        exit_code = run_compiled_brief.main()

    assert exit_code == 0
    assert "telegram is down" in caplog.text


def test_touch_failure_is_logged_not_raised(
    tmp_path, write_vault_manifest, monkeypatch, caplog
):
    """ТЗ requirement (defect 2 in code review): a memory-engine failure
    while touching the brief's source page must not take down the brief
    itself -- the file is already written and Telegram delivery must still
    be attempted."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
    )
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        run_compiled_brief, "write_validated_vault_markdown", lambda *a, **k: None
    )

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("memory engine is down")

    monkeypatch.setattr(run_compiled_brief, "QmdService", _boom)

    send_calls: list[str] = []
    monkeypatch.setattr(
        run_compiled_brief,
        "send_telegram_text_sync",
        lambda text, **kwargs: send_calls.append(text),
    )
    monkeypatch.setattr(
        "sys.argv", ["prog", "--type", "topic", "--query", "quantum-widgets"]
    )

    with caplog.at_level(logging.WARNING):
        exit_code = run_compiled_brief.main()

    assert exit_code == 0
    assert "memory engine is down" in caplog.text
    assert len(send_calls) == 1


def test_brief_path_selected_while_holding_vault_write_lock(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Code-review defect 5: picking the counter-suffixed output path must
    happen while holding the vault write lock, not before. Computing it
    first let two near-simultaneous CLI runs both see the same suffix as
    free and then both write it, the second silently overwriting the
    first. Verified structurally: a probe lock records whether it is held
    at the moment ``_brief_path`` runs.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/quantum-widgets.md",
        domain="topics",
        title="Квантовые виджеты",
    )
    _patch_settings(monkeypatch, vault)

    lock_held = False
    observed_while_holding_lock: list[bool] = []

    @contextmanager
    def _probe_lock(vault_path: Path):  # type: ignore[no-untyped-def]
        nonlocal lock_held
        del vault_path
        lock_held = True
        try:
            yield SimpleNamespace()
        finally:
            lock_held = False

    real_brief_path = run_compiled_brief._brief_path

    def _spy_brief_path(vault_path, result, today):  # type: ignore[no-untyped-def]
        observed_while_holding_lock.append(lock_held)
        return real_brief_path(vault_path, result, today)

    monkeypatch.setattr(run_compiled_brief, "vault_write_lock", _probe_lock)
    monkeypatch.setattr(run_compiled_brief, "_brief_path", _spy_brief_path)
    monkeypatch.setattr(
        run_compiled_brief, "write_validated_vault_markdown", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_compiled_brief, "send_telegram_text_sync", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_compiled_brief,
        "QmdService",
        lambda vault_path: SimpleNamespace(touch_notes=lambda targets: None),
    )
    monkeypatch.setattr(
        "sys.argv", ["prog", "--type", "topic", "--query", "quantum-widgets"]
    )

    exit_code = run_compiled_brief.main()

    assert exit_code == 0
    assert observed_while_holding_lock == [True]


# --- collision handling (ТЗ 7.6 step 4) -----------------------------------


def test_brief_path_collision_gets_counter_suffix(tmp_path, write_vault_manifest):
    """Mirrors ``CompiledBriefingService.file_output_artifact``'s
    counter-suffix pattern: a same-day, same-type, same-slug collision must
    not overwrite the earlier brief."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    directory = vault / "summaries" / "briefs"
    directory.mkdir(parents=True)
    existing = directory / f"{DAY.isoformat()}-topic-quantum-widgets.md"
    existing.write_text("already here", encoding="utf-8")

    result = BriefResult(
        brief_type="topic",
        domain="topics",
        slug="quantum-widgets",
        source_rel_path="compiled/topics/quantum-widgets.md",
        title="Квантовые виджеты",
        markdown="**Бриф по теме — Квантовые виджеты**",
    )

    path = run_compiled_brief._brief_path(vault, result, DAY)

    assert path == directory / f"{DAY.isoformat()}-topic-quantum-widgets-1.md"
