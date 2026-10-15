"""Tests for the T5 weekly wiki-care satellite (``compiled_wiki_care.py``).

Mirrors ``test_compiled_fact_check.py``'s shape for the same kind of
satellite module: pure/deterministic helpers are tested directly, the
model-consuming call sites are exercised through a real, temporary
``CompiledBriefingService`` with ``_run_json_dict_prompt`` monkeypatched
per call (dispatched on ``error_context``), and orchestration
(``run_weekly_wiki_care``) is tested with each of the four actions
monkeypatched out so only its own budget/interval/journal/ops-log wiring is
under test.
"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain.services import compiled_wiki_care
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.web_content import WebContentResult

# --- test helpers -----------------------------------------------------------


def _wiki_service(vault: Path, write_vault_manifest) -> CompiledBriefingService:
    write_vault_manifest(vault)
    return CompiledBriefingService(vault)


def _wiki_page_text(
    *,
    title: str,
    sources: list[str] | None = None,
    related: list[str] | None = None,
    body: str = "",
) -> str:
    """One minimal compiled page, in the shape ``_render_briefing`` itself
    produces: an H1 title, an optional ``## Related Pages`` section, and a
    ``## Sources`` section. Frontmatter fields match ``_page_text`` in
    ``tests/test_compiled_fact_check.py`` (the "default" manifest profile)."""
    frontmatter = (
        "---\n"
        "type: compiled-briefing\n"
        "domain: topics\n"
        'description: "Тестовая страница"\n'
        "last_accessed: 2026-07-01\n"
        "relevance: 0.80\n"
        "tier: active\n"
        "---\n\n"
    )
    lines = [f"# {title}", ""]
    if body:
        lines.extend([body, ""])
    if related:
        lines.append("## Related Pages")
        lines.extend(f"- {item}" for item in related)
        lines.append("")
    lines.append("## Sources")
    if sources:
        lines.extend(f"- [[{path}]]" for path in sources)
    else:
        lines.append("- (none)")
    lines.append("")
    return frontmatter + "\n".join(lines) + "\n"


def _write_page(vault: Path, rel_path: str, text: str) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_page(vault: Path, rel_path: str) -> str:
    return (vault / rel_path).read_text(encoding="utf-8")


def _wiki_page(
    rel_path: str,
    title: str,
    *,
    sources: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    body_lower: str = "",
) -> "compiled_wiki_care._WikiCarePage":
    return compiled_wiki_care._WikiCarePage(
        rel_path=rel_path,
        title=title,
        sources=frozenset(sources),
        related_targets=frozenset(related),
        scrubbed_body_lower=body_lower,
    )


# --- _ModelCallBudget --------------------------------------------------------


def test_model_call_budget_refuses_once_limit_reached() -> None:
    budget = compiled_wiki_care._ModelCallBudget(2)

    assert budget.spend() is True
    assert budget.spend() is True
    assert budget.spend() is False
    assert budget.used == 2


def test_model_call_budget_is_unlimited_when_limit_is_none() -> None:
    budget = compiled_wiki_care._ModelCallBudget(None)

    for _ in range(20):
        assert budget.spend() is True
    assert budget.used == 20


# --- _should_run --------------------------------------------------------


def test_should_run_true_when_never_run_before() -> None:
    assert compiled_wiki_care._should_run({}, today=date(2026, 9, 25)) is True


def test_should_run_true_on_malformed_last_run() -> None:
    journal = {"last_run": "not-a-date"}
    assert compiled_wiki_care._should_run(journal, today=date(2026, 9, 25)) is True


def test_should_run_false_inside_the_7_day_window() -> None:
    journal = {"last_run": "2026-09-20"}
    assert compiled_wiki_care._should_run(journal, today=date(2026, 9, 25)) is False


def test_should_run_true_once_7_days_have_passed() -> None:
    journal = {"last_run": "2026-09-18"}
    assert compiled_wiki_care._should_run(journal, today=date(2026, 9, 25)) is True


# --- journal read/write -------------------------------------------------


def test_read_journal_returns_empty_dict_when_missing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    assert compiled_wiki_care._read_journal(vault) == {}


def test_write_journal_ok_status_advances_last_run(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    compiled_wiki_care._write_wiki_care_journal(
        vault,
        today=date(2026, 9, 25),
        status="ok",
        previous_last_run="2026-09-01",
        links_added=2,
        pages_queued=1,
        questions_answered=1,
        articles_imported=0,
        changed_paths=["compiled/topics/a.md"],
        errors=[],
        model_calls_used=4,
    )

    journal = compiled_wiki_care._read_journal(vault)
    assert journal["last_run"] == "2026-09-25"
    assert journal["status"] == "ok"
    assert journal["links_added"] == 2
    assert journal["changed_paths"] == ["compiled/topics/a.md"]


def test_write_journal_failed_status_preserves_previous_last_run(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    compiled_wiki_care._write_wiki_care_journal(
        vault,
        today=date(2026, 9, 25),
        status="failed",
        previous_last_run="2026-09-01",
        links_added=0,
        pages_queued=0,
        questions_answered=0,
        articles_imported=0,
        changed_paths=[],
        errors=["RuntimeError: boom"],
        model_calls_used=1,
    )

    journal = compiled_wiki_care._read_journal(vault)
    assert journal["status"] == "failed"
    assert journal["last_run"] == "2026-09-01"


# --- _find_missing_link_candidates ---------------------------------------


def test_find_missing_link_candidates_shared_sources_outrank_mentions() -> None:
    page_a = _wiki_page(
        "compiled/topics/a.md",
        "Alpha Project",
        sources=("daily/2026-08-01.md", "daily/2026-08-02.md"),
    )
    page_b = _wiki_page(
        "compiled/topics/b.md",
        "Beta Notes",
        sources=("daily/2026-08-01.md", "daily/2026-08-02.md"),
    )
    page_c = _wiki_page(
        "compiled/topics/c.md",
        "Gamma Plan",
        body_lower="see the alpha project writeup",
    )

    candidates = compiled_wiki_care._find_missing_link_candidates(
        [page_a, page_b, page_c]
    )

    assert len(candidates) == 2
    assert candidates[0].strength > candidates[1].strength
    assert "shared sources" in candidates[0].signal
    assert candidates[1].signal == "title mentioned in body without a link"


def test_find_missing_link_candidates_is_case_insensitive() -> None:
    page_a = _wiki_page("compiled/topics/a.md", "Aurora Rollout")
    page_b = _wiki_page(
        "compiled/topics/b.md",
        "Beta Notes",
        body_lower="see the aurora rollout status",
    )

    candidates = compiled_wiki_care._find_missing_link_candidates([page_a, page_b])

    assert len(candidates) == 1


def test_find_missing_link_candidates_ignores_titles_below_min_length() -> None:
    page_a = _wiki_page("compiled/topics/a.md", "MVP")  # 3 chars
    page_b = _wiki_page(
        "compiled/topics/b.md", "Beta Notes", body_lower="the mvp shipped today"
    )

    candidates = compiled_wiki_care._find_missing_link_candidates([page_a, page_b])

    assert candidates == []


def test_find_missing_link_candidates_skips_already_linked_pair() -> None:
    page_a = _wiki_page(
        "compiled/topics/a.md",
        "Aurora Rollout",
        related=("compiled/topics/b",),
    )
    page_b = _wiki_page(
        "compiled/topics/b.md",
        "Beta Notes",
        body_lower="see the aurora rollout status",
    )

    candidates = compiled_wiki_care._find_missing_link_candidates([page_a, page_b])

    assert candidates == []


def test_collect_wiki_care_pages_strips_wikilinks_and_excluded_sections(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        (
            "---\ntype: compiled-briefing\nlast_accessed: 2026-07-01\n"
            "relevance: 0.8\ntier: active\n---\n\n"
            "# Alpha Project\n\n"
            "## Current State\n"
            "Already links to [[compiled/topics/b|Beta Notes]]. "
            "Separately mentions gamma plan in plain text.\n\n"
            "## Sources\n- [[daily/2026-08-01.md]]\n\n"
            "## Sources That Shaped This Page\n"
            "- placeholder row mentioning gamma plan here\n\n"
        ),
    )

    pages = compiled_wiki_care._collect_wiki_care_pages(service)

    assert len(pages) == 1
    page = pages[0]
    # an existing wikilink is stripped whole (target and display text alike),
    # so an already-linked mention never counts as an "unlinked mention"
    assert "beta notes" not in page.scrubbed_body_lower
    assert "[[compiled/topics/b" not in page.scrubbed_body_lower
    # a plain-text mention outside any wikilink and outside an excluded
    # section survives the scrub
    assert "gamma plan in plain text" in page.scrubbed_body_lower
    # the excluded section's own body is gone entirely, heading included
    assert "placeholder row" not in page.scrubbed_body_lower


def test_collect_wiki_care_pages_counts_body_links_as_existing_links(
    tmp_path: Path, write_vault_manifest
) -> None:
    """A body link -- with or without ``.md`` or an anchor -- already links
    the pair, so it must not come back as a missing-link candidate."""
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md", "daily/2026-08-02.md"],
            body="See [[compiled/topics/b.md#Summary]].",
        ),
    )
    _write_page(
        vault,
        "compiled/topics/b.md",
        _wiki_page_text(
            title="Beta Notes",
            sources=["daily/2026-08-01.md", "daily/2026-08-02.md"],
            body="Beta body.",
        ),
    )

    pages = compiled_wiki_care._collect_wiki_care_pages(service)

    assert "compiled/topics/b" in pages[0].related_targets
    assert compiled_wiki_care._find_missing_link_candidates(pages) == []


# --- _link_related_page --------------------------------------------------


def test_link_related_page_creates_section_before_sources(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(title="Alpha Project", sources=["daily/2026-08-01.md"]),
    )

    written, error = compiled_wiki_care._link_related_page(
        service,
        rel_path="compiled/topics/a.md",
        target_rel_path="compiled/topics/b.md",
        target_title="Beta Notes",
    )

    assert written is True
    assert error is None
    text = _read_page(vault, "compiled/topics/a.md")
    assert "## Related Pages\n- [[compiled/topics/b|Beta Notes]]" in text
    assert text.index("## Related Pages") < text.index("## Sources")


def test_link_related_page_appends_to_existing_section(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md"],
            related=["[[compiled/topics/c|Gamma Plan]]"],
        ),
    )

    written, error = compiled_wiki_care._link_related_page(
        service,
        rel_path="compiled/topics/a.md",
        target_rel_path="compiled/topics/b.md",
        target_title="Beta Notes",
    )

    assert written is True
    assert error is None
    text = _read_page(vault, "compiled/topics/a.md")
    assert "[[compiled/topics/c|Gamma Plan]]" in text
    assert "[[compiled/topics/b|Beta Notes]]" in text


def test_link_related_page_is_idempotent(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(title="Alpha Project", sources=["daily/2026-08-01.md"]),
    )
    compiled_wiki_care._link_related_page(
        service,
        rel_path="compiled/topics/a.md",
        target_rel_path="compiled/topics/b.md",
        target_title="Beta Notes",
    )
    text_after_first_write = _read_page(vault, "compiled/topics/a.md")

    written, error = compiled_wiki_care._link_related_page(
        service,
        rel_path="compiled/topics/a.md",
        target_rel_path="compiled/topics/b.md",
        target_title="Beta Notes",
    )

    assert written is False
    assert error is None
    assert _read_page(vault, "compiled/topics/a.md") == text_after_first_write


def test_link_related_page_missing_page_reports_error(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)

    written, error = compiled_wiki_care._link_related_page(
        service,
        rel_path="compiled/topics/missing.md",
        target_rel_path="compiled/topics/b.md",
        target_title="Beta Notes",
    )

    assert written is False
    assert error == "page missing"


# --- _apply_missing_links (batching/budget, ПОПРАВКА 2) -------------------


def _fake_candidates(count: int) -> list["compiled_wiki_care._LinkCandidate"]:
    return [
        compiled_wiki_care._LinkCandidate(
            rel_path_a=f"compiled/topics/a{i}.md",
            rel_path_b=f"compiled/topics/b{i}.md",
            title_a=f"Alpha Page {i}",
            title_b=f"Beta Page {i}",
            signal="test",
            strength=1,
        )
        for i in range(count)
    ]


def test_apply_missing_links_caps_at_3_batches_when_budget_limited(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    monkeypatch.setattr(
        compiled_wiki_care,
        "_find_missing_link_candidates",
        lambda pages: _fake_candidates(25),  # 4 batches worth (batch size 8)
    )
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda *, prompt, timeout, error_context, json_example: (
            prompt_calls.append(error_context) or {"confirmed": []}
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(None)  # only the cap matters here

    result = compiled_wiki_care._apply_missing_links(service, budget, no_limit=False)

    assert len(prompt_calls) == compiled_wiki_care.MISSING_LINK_MAX_BATCHES_LIMITED
    assert result == {"links_added": 0, "changed_paths": [], "errors": []}


def test_apply_missing_links_has_no_batch_cap_when_no_limit(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    monkeypatch.setattr(
        compiled_wiki_care,
        "_find_missing_link_candidates",
        lambda pages: _fake_candidates(25),
    )
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda *, prompt, timeout, error_context, json_example: (
            prompt_calls.append(error_context) or {"confirmed": []}
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(None)

    compiled_wiki_care._apply_missing_links(service, budget, no_limit=True)

    assert len(prompt_calls) == 4  # ceil(25 / MISSING_LINK_BATCH_SIZE)


def test_apply_missing_links_stops_on_budget_exhaustion_even_with_no_limit(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    monkeypatch.setattr(
        compiled_wiki_care,
        "_find_missing_link_candidates",
        lambda pages: _fake_candidates(25),
    )
    prompt_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda *, prompt, timeout, error_context, json_example: (
            prompt_calls.append(error_context) or {"confirmed": []}
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(2)

    compiled_wiki_care._apply_missing_links(service, budget, no_limit=True)

    assert len(prompt_calls) == 2
    assert budget.used == 2


def test_apply_missing_links_writes_mutual_links_when_confirmed(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md", "daily/2026-08-02.md"],
        ),
    )
    _write_page(
        vault,
        "compiled/topics/b.md",
        _wiki_page_text(
            title="Beta Notes",
            sources=["daily/2026-08-01.md", "daily/2026-08-02.md"],
        ),
    )

    def fake_prompt(*, prompt, timeout, error_context, json_example):
        assert error_context == "wiki-care-related-links"
        return {"confirmed": [{"index": 1, "relevant": True, "reason": "same rollout"}]}

    monkeypatch.setattr(service, "_run_json_dict_prompt", fake_prompt)
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._apply_missing_links(service, budget, no_limit=False)

    assert result["links_added"] == 2
    assert set(result["changed_paths"]) == {
        "compiled/topics/a.md",
        "compiled/topics/b.md",
    }
    assert "[[compiled/topics/b|Beta Notes]]" in _read_page(
        vault, "compiled/topics/a.md"
    )
    assert "[[compiled/topics/a|Alpha Project]]" in _read_page(
        vault, "compiled/topics/b.md"
    )


# --- Action 2: missing pages ------------------------------------------------


def test_find_missing_page_topics_surfaces_dangling_wikilink(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md"],
            body="See [[compiled/people/ghost-person.md]] for context.",
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(0)  # no MOC catalog either

    topics = compiled_wiki_care._find_missing_page_topics(service, budget)

    assert {"domain": "people", "title": "ghost person"} in topics


def test_find_missing_page_topics_ignores_existing_page_linked_without_md(
    tmp_path: Path, write_vault_manifest
) -> None:
    """Related Pages and the catalog link pages without ``.md``; such a link
    to a page that exists is not dangling."""
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md"],
            body=(
                "See [[compiled/topics/beta|Beta Notes]], "
                "[[compiled/topics/beta#Summary]] and [[compiled/people/ghost]]."
            ),
        ),
    )
    _write_page(
        vault,
        "compiled/topics/beta.md",
        _wiki_page_text(
            title="Beta Notes",
            sources=["daily/2026-08-02.md"],
            body="Beta body.",
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(0)

    topics = compiled_wiki_care._find_missing_page_topics(service, budget)

    assert topics == [{"domain": "people", "title": "ghost"}]


def test_find_missing_page_topics_rejects_invalid_domain_dangling_link(
    tmp_path: Path, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    _write_page(
        vault,
        "compiled/topics/a.md",
        _wiki_page_text(
            title="Alpha Project",
            sources=["daily/2026-08-01.md"],
            body="See [[compiled/badomain/x.md]] for context.",
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(0)

    topics = compiled_wiki_care._find_missing_page_topics(service, budget)

    assert topics == []


def test_find_missing_page_topics_rejects_model_suggested_invalid_domain(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    (vault / "MOC").mkdir(parents=True)
    (vault / "MOC" / "compiled-index.md").write_text("# Catalog\n", encoding="utf-8")
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda **kwargs: {
            "topics": [
                {"domain": "bogus", "title": "Should Be Rejected"},
                {"domain": "topics", "title": "Valid Topic"},
            ]
        },
    )
    budget = compiled_wiki_care._ModelCallBudget(None)

    topics = compiled_wiki_care._find_missing_page_topics(service, budget)

    assert topics == [{"domain": "topics", "title": "Valid Topic"}]


def test_seed_missing_pages_skips_topic_with_no_sources(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    enqueue_calls: list[dict] = []

    def fake_enqueue_refresh(*, source_path, source_excerpt, max_updates):
        enqueue_calls.append(
            {
                "source_path": source_path,
                "source_excerpt": source_excerpt,
                "max_updates": max_updates,
            }
        )
        return {"queued": True}

    def fake_qmd_query(query, *, limit):
        if query == "No Sources Topic":
            return {"results": []}
        return {
            "results": [{"file": "daily/2026-08-01.md", "snippet": "relevant excerpt"}]
        }

    monkeypatch.setattr(service, "enqueue_refresh", fake_enqueue_refresh)
    monkeypatch.setattr(service.qmd, "query", fake_qmd_query)
    topics = [
        {"domain": "topics", "title": "No Sources Topic"},
        {"domain": "topics", "title": "Found Topic"},
    ]

    result = compiled_wiki_care._seed_missing_pages(service, topics)

    assert result == {"queued": 1, "errors": []}
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]["source_path"] == "daily/2026-08-01.md"
    assert enqueue_calls[0]["max_updates"] == 2
    assert "Found Topic" in enqueue_calls[0]["source_excerpt"]


def test_seed_missing_pages_records_qmd_query_errors(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)

    def fake_qmd_query(query, *, limit):
        raise RuntimeError("qmd index unavailable")

    monkeypatch.setattr(service.qmd, "query", fake_qmd_query)

    result = compiled_wiki_care._seed_missing_pages(
        service, [{"domain": "topics", "title": "Anything"}]
    )

    assert result["queued"] == 0
    assert result["errors"] == ["Anything: qmd index unavailable"]


# --- Action 3: vault-gap questions ------------------------------------------


def test_answer_vault_gaps_counts_only_filed_artifact_answers() -> None:
    answers = {
        "Q1?": {"filed_artifact_path": "summaries/answers/q1.md"},
        "Q2?": {"answer": "text only, nothing filed"},
        "Q3?": {"filed_artifact_path": "   "},
    }
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._answer_vault_gaps(
        list(answers), budget, answer_question=lambda q: answers[q]
    )

    assert result["questions_answered"] == 1
    assert result["changed_paths"] == ["summaries/answers/q1.md"]
    assert result["errors"] == []


def test_answer_vault_gaps_stops_on_budget_exhaustion() -> None:
    calls: list[str] = []

    def fake_answer_question(question):
        calls.append(question)
        return {"filed_artifact_path": "x.md"}

    budget = compiled_wiki_care._ModelCallBudget(1)

    result = compiled_wiki_care._answer_vault_gaps(
        ["Q1?", "Q2?", "Q3?"], budget, answer_question=fake_answer_question
    )

    assert calls == ["Q1?"]
    assert result["questions_answered"] == 1


def test_answer_vault_gaps_records_error_and_continues() -> None:
    def fake_answer_question(question):
        if question == "Q1?":
            raise RuntimeError("boom")
        return {"filed_artifact_path": "q2.md"}

    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._answer_vault_gaps(
        ["Q1?", "Q2?"], budget, answer_question=fake_answer_question
    )

    assert result["questions_answered"] == 1
    assert result["errors"] == ["Q1?: boom"]


# --- Action 4: web search ----------------------------------------------


def test_run_web_search_action_skips_entirely_without_tavily_key(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)

    def fail_prompt(**kwargs):
        raise AssertionError("must not call the model without a Tavily key")

    def fail_search(*args, **kwargs):
        raise AssertionError("must not search without a Tavily key")

    monkeypatch.setattr(service, "_run_json_dict_prompt", fail_prompt)
    monkeypatch.setattr(compiled_wiki_care, "tavily_search", fail_search)
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._run_web_search_action(
        service,
        budget,
        tavily_api_key="",
        no_limit=False,
        content_language="ru",
        ai_cli="claude",
    )

    assert result == {"articles_imported": 0, "changed_paths": [], "errors": []}
    assert budget.used == 0


def _stub_web_search_prompts(monkeypatch, service, *, num_results: int = 1):
    def fake_prompt(*, prompt, timeout, error_context, json_example):
        if error_context == "wiki-care-web-search-queries":
            return {"queries": ["aurora rollout status"]}
        if error_context == "wiki-care-web-search-selection":
            return {
                "selected": [
                    {"index": i + 1, "reason": "relevant"} for i in range(num_results)
                ]
            }
        raise AssertionError(f"unexpected error_context: {error_context}")

    monkeypatch.setattr(service, "_run_json_dict_prompt", fake_prompt)


def test_run_web_search_action_imports_under_auto_subdir_and_logs_ops_entry(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    (vault / "MOC").mkdir(parents=True)
    (vault / "MOC" / "compiled-index.md").write_text("# Catalog\n", encoding="utf-8")
    _stub_web_search_prompts(monkeypatch, service)
    monkeypatch.setattr(
        compiled_wiki_care,
        "tavily_search",
        lambda query, *, api_key, max_results: [
            {
                "url": "https://example.com/aurora",
                "title": "Aurora status",
                "content": "c",
            }
        ],
    )
    captured: dict = {}

    class FakeWebArchiveService:
        def __init__(self, vault_path, *, content_language, ai_cli, notes_subdir):
            captured["notes_subdir"] = notes_subdir

        def archive_page(self, content_result, **kwargs):
            return SimpleNamespace(note_path="imports/web/auto/2026/09/aurora.md")

    monkeypatch.setattr(compiled_wiki_care, "WebArchiveService", FakeWebArchiveService)
    monkeypatch.setattr(
        compiled_wiki_care,
        "extract_web_content",
        lambda url, *, config, timeout, allowed_url: WebContentResult(
            url=url, title="Aurora status", content="Full article text", source="direct"
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._run_web_search_action(
        service,
        budget,
        tavily_api_key="tvly-secret",
        no_limit=False,
        content_language="ru",
        ai_cli="claude",
    )

    assert result["articles_imported"] == 1
    assert result["changed_paths"] == ["imports/web/auto/2026/09/aurora.md"]
    assert captured["notes_subdir"] == compiled_wiki_care.IMPORTS_WEB_AUTO_SUBDIR
    log_line = (vault / ".session" / "log.md").read_text(encoding="utf-8").strip()
    assert "[web-search]" in log_line
    assert "aurora.md" in log_line


def test_run_web_search_action_rejects_non_public_url_before_extracting(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    (vault / "MOC").mkdir(parents=True)
    (vault / "MOC" / "compiled-index.md").write_text("# Catalog\n", encoding="utf-8")
    _stub_web_search_prompts(monkeypatch, service)
    monkeypatch.setattr(
        compiled_wiki_care,
        "tavily_search",
        lambda query, *, api_key, max_results: [
            {"url": "http://localhost/internal", "title": "Internal", "content": "c"}
        ],
    )

    def must_not_extract(*args, **kwargs):
        raise AssertionError("must not fetch a private/localhost URL")

    monkeypatch.setattr(compiled_wiki_care, "extract_web_content", must_not_extract)
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._run_web_search_action(
        service,
        budget,
        tavily_api_key="tvly-secret",
        no_limit=False,
        content_language="ru",
        ai_cli="claude",
    )

    assert result == {"articles_imported": 0, "changed_paths": [], "errors": []}


@pytest.mark.parametrize(
    ("no_limit", "expected_limit"),
    [
        (False, compiled_wiki_care.DEFAULT_WIKI_CARE_WEB_IMPORT_LIMIT),
        (True, compiled_wiki_care.NO_LIMIT_WIKI_CARE_WEB_IMPORT_LIMIT),
    ],
)
def test_run_web_search_action_respects_import_limit(
    tmp_path: Path, write_vault_manifest, monkeypatch, no_limit, expected_limit
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    (vault / "MOC").mkdir(parents=True)
    (vault / "MOC" / "compiled-index.md").write_text("# Catalog\n", encoding="utf-8")
    num_results = expected_limit + 2
    _stub_web_search_prompts(monkeypatch, service, num_results=num_results)
    urls = [f"https://example.com/page{i}" for i in range(num_results)]
    monkeypatch.setattr(
        compiled_wiki_care,
        "tavily_search",
        lambda query, *, api_key, max_results: [
            {"url": url, "title": "T", "content": "c"} for url in urls
        ],
    )
    archived_urls: list[str] = []

    class FakeWebArchiveService:
        def __init__(self, vault_path, *, content_language, ai_cli, notes_subdir):
            pass

        def archive_page(self, content_result, **kwargs):
            archived_urls.append(content_result.url)
            note_path = f"imports/web/auto/{len(archived_urls)}.md"
            return SimpleNamespace(note_path=note_path)

    monkeypatch.setattr(compiled_wiki_care, "WebArchiveService", FakeWebArchiveService)
    monkeypatch.setattr(
        compiled_wiki_care,
        "extract_web_content",
        lambda url, *, config, timeout, allowed_url: WebContentResult(
            url=url, title="T", content="c", source="direct"
        ),
    )
    budget = compiled_wiki_care._ModelCallBudget(None)

    result = compiled_wiki_care._run_web_search_action(
        service,
        budget,
        tavily_api_key="tvly-secret",
        no_limit=no_limit,
        content_language="ru",
        ai_cli="claude",
    )

    assert len(archived_urls) == expected_limit
    assert result["articles_imported"] == expected_limit


# --- _drain_to_completion -------------------------------------------------


def test_drain_to_completion_stops_when_a_round_drains_nothing(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    rounds = [
        {"drained": 2, "updated": ["compiled/topics/a.md"], "errors": []},
        {
            "drained": 1,
            "updated": ["compiled/topics/b.md", "compiled/topics/a.md"],
            "errors": [],
        },
        {"drained": 0, "updated": [], "errors": []},
    ]
    calls: list[dict] = []

    def fake_drain_queue(*, force, max_events):
        calls.append({"force": force, "max_events": max_events})
        return rounds[len(calls) - 1]

    monkeypatch.setattr(service, "drain_queue", fake_drain_queue)

    result = compiled_wiki_care._drain_to_completion(service)

    assert len(calls) == 3
    assert all(call == {"force": True, "max_events": 8} for call in calls)
    assert result["drained"] == 3
    assert result["updated"] == ["compiled/topics/a.md", "compiled/topics/b.md"]
    assert result["errors"] == []


def test_drain_to_completion_stops_immediately_on_worker_busy(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = _wiki_service(vault, write_vault_manifest)
    calls: list[int] = []

    def fake_drain_queue(*, force, max_events):
        calls.append(1)
        return {"drained": 0, "updated": [], "errors": ["worker-busy"]}

    monkeypatch.setattr(service, "drain_queue", fake_drain_queue)

    result = compiled_wiki_care._drain_to_completion(service)

    assert len(calls) == 1
    assert result["errors"] == ["worker-busy"]


# --- run_weekly_wiki_care orchestration -----------------------------------


def _patch_all_actions_as_no_work(monkeypatch) -> None:
    monkeypatch.setattr(
        compiled_wiki_care,
        "_apply_missing_links",
        lambda service, budget, *, no_limit: {
            "links_added": 0,
            "changed_paths": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        compiled_wiki_care, "_find_missing_page_topics", lambda service, budget: []
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_seed_missing_pages",
        lambda service, topics: {"queued": 0, "errors": []},
    )
    monkeypatch.setattr(
        compiled_wiki_care, "_find_vault_gap_questions", lambda service, budget: []
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_answer_vault_gaps",
        lambda questions, budget, *, answer_question: {
            "questions_answered": 0,
            "changed_paths": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_run_web_search_action",
        lambda service, budget, **kwargs: {
            "articles_imported": 0,
            "changed_paths": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "d_brain.services.compiled_index.refresh_compiled_index",
        lambda vault_path, **kwargs: False,
    )


def test_run_weekly_wiki_care_skips_within_interval(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "compile-wiki-care.json").write_text(
        json.dumps({"last_run": "2026-09-20"}), encoding="utf-8"
    )

    def must_not_construct_service(*args, **kwargs):
        raise AssertionError("must not build a service when the run is skipped")

    monkeypatch.setattr(
        compiled_wiki_care, "CompiledBriefingService", must_not_construct_service
    )

    result = compiled_wiki_care.run_weekly_wiki_care(
        vault,
        answer_question=lambda q: {},
        today=date(2026, 9, 25),
        no_limit=False,
    )

    assert result == {
        "status": "skipped-interval",
        "links_added": 0,
        "pages_created": 0,
        "questions_answered": 0,
        "articles_imported": 0,
        "changed_paths": [],
        "errors": [],
        "model_calls_used": 0,
    }


def test_run_weekly_wiki_care_no_limit_bypasses_interval_and_drains_queue(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "compile-wiki-care.json").write_text(
        json.dumps({"last_run": "2026-09-24"}), encoding="utf-8"
    )
    _patch_all_actions_as_no_work(monkeypatch)
    drain_calls: list[int] = []
    def fake_drain(service):
        drain_calls.append(1)
        return {"drained": 0, "updated": [], "errors": []}

    monkeypatch.setattr(compiled_wiki_care, "_drain_to_completion", fake_drain)

    result = compiled_wiki_care.run_weekly_wiki_care(
        vault,
        answer_question=lambda q: {},
        today=date(2026, 9, 25),
        no_limit=True,
    )

    assert result["status"] == "no-work"
    assert len(drain_calls) == 1


def test_run_weekly_wiki_care_normal_mode_never_drains_the_queue(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _patch_all_actions_as_no_work(monkeypatch)

    def must_not_drain(service):
        raise AssertionError("a normal run must not drain the refresh queue")

    monkeypatch.setattr(compiled_wiki_care, "_drain_to_completion", must_not_drain)

    result = compiled_wiki_care.run_weekly_wiki_care(
        vault,
        answer_question=lambda q: {},
        today=date(2026, 9, 25),
        no_limit=False,
    )

    assert result["status"] == "no-work"


def test_run_weekly_wiki_care_reports_ok_status_and_counters(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    monkeypatch.setattr(
        compiled_wiki_care,
        "_apply_missing_links",
        lambda service, budget, *, no_limit: {
            "links_added": 2,
            "changed_paths": ["compiled/topics/a.md"],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_find_missing_page_topics",
        lambda service, budget: [{"domain": "topics", "title": "X"}],
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_seed_missing_pages",
        lambda service, topics: {"queued": 1, "errors": []},
    )
    monkeypatch.setattr(
        compiled_wiki_care, "_find_vault_gap_questions", lambda service, budget: ["Q?"]
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_answer_vault_gaps",
        lambda questions, budget, *, answer_question: {
            "questions_answered": 1,
            "changed_paths": ["summaries/answers/q.md"],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        compiled_wiki_care,
        "_run_web_search_action",
        lambda service, budget, **kwargs: {
            "articles_imported": 1,
            "changed_paths": ["imports/web/auto/x.md"],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "d_brain.services.compiled_index.refresh_compiled_index",
        lambda vault_path, **kwargs: True,
    )

    result = compiled_wiki_care.run_weekly_wiki_care(
        vault,
        answer_question=lambda q: {},
        today=date(2026, 9, 25),
        no_limit=False,
    )

    assert result["status"] == "ok"
    assert result["links_added"] == 2
    assert result["pages_created"] == 1
    assert result["questions_answered"] == 1
    assert result["articles_imported"] == 1
    assert set(result["changed_paths"]) == {
        "compiled/topics/a.md",
        "summaries/answers/q.md",
        "imports/web/auto/x.md",
    }
    assert result["compiled_index_written"] is True
    journal = json.loads(
        (vault / ".session" / "compile-wiki-care.json").read_text(encoding="utf-8")
    )
    assert journal["last_run"] == "2026-09-25"
    assert journal["status"] == "ok"


def test_run_weekly_wiki_care_exception_preserves_last_run_and_logs_failure(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "compile-wiki-care.json").write_text(
        json.dumps({"last_run": "2026-09-01"}), encoding="utf-8"
    )

    def boom(service, budget, *, no_limit):
        raise RuntimeError("boom")

    monkeypatch.setattr(compiled_wiki_care, "_apply_missing_links", boom)

    with pytest.raises(RuntimeError, match="boom"):
        compiled_wiki_care.run_weekly_wiki_care(
            vault,
            answer_question=lambda q: {},
            today=date(2026, 9, 25),
            no_limit=False,
        )

    journal = json.loads(
        (vault / ".session" / "compile-wiki-care.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "failed"
    assert journal["last_run"] == "2026-09-01"
    log_line = (vault / ".session" / "log.md").read_text(encoding="utf-8").strip()
    assert log_line.endswith("[wiki-care] ошибка: RuntimeError")


def test_run_weekly_wiki_care_budget_exhausted_mid_run_keeps_earlier_work(
    tmp_path: Path, write_vault_manifest, monkeypatch
) -> None:
    """Action 1 spends the whole 10-call budget: the later actions must not
    reach the model, and the run still counts as a success with the links
    action 1 already wrote."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "MOC").mkdir(parents=True)
    (vault / "MOC" / "compiled-index.md").write_text("# Catalog\n", encoding="utf-8")

    def _spend_everything(service, budget, *, no_limit):  # noqa: ANN001, ANN202
        while budget.spend():
            pass
        return {
            "links_added": 1,
            "changed_paths": ["compiled/topics/a.md"],
            "errors": [],
        }

    def _no_model(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("model called after the budget ran out")

    monkeypatch.setattr(compiled_wiki_care, "_apply_missing_links", _spend_everything)
    monkeypatch.setattr(
        compiled_wiki_care.CompiledBriefingService, "_run_json_dict_prompt", _no_model
    )

    result = compiled_wiki_care.run_weekly_wiki_care(
        vault,
        answer_question=_no_model,
        tavily_api_key="tvly-key",
        today=date(2026, 9, 25),
    )

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert result["links_added"] == 1
    assert result["model_calls_used"] == 10
