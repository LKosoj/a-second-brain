"""Tests for deterministic question-answer provenance (ТЗ 7.4, 4.4).

``build_question_provenance`` is pure and read-only (see module docstring),
so every test here only assembles a temporary ``compiled/**`` tree -- no
model is called and no vault write path is exercised.
"""

from __future__ import annotations

from pathlib import Path

from d_brain.services.compiled_briefings import CompiledBriefingCandidate
from d_brain.services.compiled_question_provenance import (
    QuestionProvenance,
    build_question_provenance,
)
from d_brain.services.processor import CliProcessor

DAY = "2026-08-05"


def _page_text(
    *,
    domain: str,
    title: str,
    description: str = "",
    sources_trust: str | None = None,
    conflicts_open: int | None = None,
) -> str:
    """Render one ``compiled/**`` page in the shape ``_iter_candidates`` and
    ``_frontmatter_fields`` actually parse (mirrors the fixture helpers in
    ``tests/test_compiled_briefs.py``/``tests/test_compiled_enrich_report.py``).
    """
    frontmatter = [
        "---",
        "type: compiled-briefing",
        f"domain: {domain}",
        f'description: "{description or title}"',
        "status: active",
        f"created: {DAY}",
        f"updated: {DAY}",
        "freshness_state: fresh",
        "confidence: high",
        f"last_accessed: {DAY}",
        "relevance: 0.80",
        "tier: active",
    ]
    if sources_trust is not None:
        frontmatter.append(f"sources_trust: {sources_trust}")
    if conflicts_open is not None:
        frontmatter.append(f"conflicts_open: {conflicts_open}")
    frontmatter.append("---")
    frontmatter.append("")
    body = [f"# {title}", "", "## Current State", f"State for {title}.", ""]
    return "\n".join(frontmatter) + "\n".join(body) + "\n"


def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page_text(**kwargs), encoding="utf-8")  # type: ignore[arg-type]


def test_no_compiled_pages_returns_empty_result(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)

    result = build_question_provenance(vault, "Что нового по Example Project?")

    assert result == QuestionProvenance(warning="", block="", touched_paths=())


def test_strong_trust_page_has_block_but_no_warning(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-topic.md",
        domain="topics",
        title="Example Topic",
        sources_trust="own",
        conflicts_open=0,
    )

    result = build_question_provenance(vault, "Что нового по Example Topic?")

    assert result.warning == ""
    assert "**Источники ответа**" in result.block
    assert "[[compiled/topics/example-topic.md|Example Topic]]" in result.block
    assert "доверие: own" in result.block
    assert "открытых конфликтов" not in result.block
    assert result.touched_paths == ("compiled/topics/example-topic.md",)


def test_weak_trust_page_triggers_warning(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-topic.md",
        domain="topics",
        title="Example Topic",
        sources_trust="forwarded",
    )

    result = build_question_provenance(vault, "Что нового по Example Topic?")

    assert result.warning.startswith("**Внимание")
    assert "forwarded" in result.warning
    assert "доверие: forwarded" in result.block


def test_open_conflicts_trigger_warning_even_with_strong_trust(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-topic.md",
        domain="topics",
        title="Example Topic",
        sources_trust="own",
        conflicts_open=2,
    )

    result = build_question_provenance(vault, "Что нового по Example Topic?")

    assert result.warning.startswith("**Внимание")
    assert "конфликт" in result.warning
    assert "forwarded" not in result.warning
    assert "открытых конфликтов: 2" in result.block


def test_missing_trust_field_shown_as_undetermined_without_warning(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-topic.md",
        domain="topics",
        title="Example Topic",
    )

    result = build_question_provenance(vault, "Что нового по Example Topic?")

    assert result.warning == ""
    assert "доверие: не определено" in result.block


def test_multiple_pages_fold_trust_to_the_weakest(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-topic-one.md",
        domain="topics",
        title="Example Topic One",
        description="Первая тема Example Topic",
        sources_trust="own",
    )
    _write_page(
        vault,
        "compiled/topics/example-topic-two.md",
        domain="topics",
        title="Example Topic Two",
        description="Вторая тема Example Topic",
        sources_trust="inferred",
    )

    result = build_question_provenance(
        vault, "Что нового по Example Topic One и Example Topic Two?"
    )

    assert result.warning.startswith("**Внимание")
    assert "inferred" in result.warning
    assert len(result.touched_paths) == 2
    assert "compiled/topics/example-topic-one.md" in result.touched_paths
    assert "compiled/topics/example-topic-two.md" in result.touched_paths


def test_explicit_candidates_are_used_instead_of_re_ranking(
    tmp_path, write_vault_manifest
):
    """ТЗ 7.4 code-review defect 2: when the caller already ranked pages at
    prompt-build time, ``build_question_provenance`` must use exactly that
    list, not re-rank against the vault (which may have changed since the
    prompt was built). A real page that *would* win the lexical re-rank for
    this question sits in the vault with strong trust; the explicit
    ``candidates`` argument names a different page with weak trust. If the
    function ignored ``candidates`` and re-ranked, the result would show the
    real page's path and "own" trust instead."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/example-real.md",
        domain="topics",
        title="Example Real Topic",
        description="Example Real Topic — совпадает с вопросом ниже",
        sources_trust="own",
        conflicts_open=0,
    )
    explicit_candidate = CompiledBriefingCandidate(
        rel_path="compiled/topics/example-explicit.md",
        domain="topics",
        slug="example-explicit",
        title="Example Explicit Topic",
        description="Передано вызывающим кодом напрямую",
        freshness_state="fresh",
        confidence="high",
        relevance=0.9,
        tier="active",
        text=_page_text(
            domain="topics",
            title="Example Explicit Topic",
            sources_trust="inferred",
            conflicts_open=1,
        ),
    )

    result = build_question_provenance(
        vault,
        "Что нового по Example Real Topic?",
        candidates=[explicit_candidate],
    )

    assert result.touched_paths == ("compiled/topics/example-explicit.md",)
    assert "example-explicit" in result.block
    assert "example-real" not in result.block
    assert "доверие: inferred" in result.block
    assert result.warning.startswith("**Внимание")


def test_respects_the_same_candidate_limit_as_the_prompt_block(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    for index in range(5):
        _write_page(
            vault,
            f"compiled/topics/example-topic-{index}.md",
            domain="topics",
            title=f"Example Topic {index}",
            description="Общая тема Example Topic для проверки лимита кандидатов",
            sources_trust="own",
        )

    result = build_question_provenance(vault, "Что нового по Example Topic?")

    assert 0 < len(result.touched_paths) <= 3


# --- warning placement (ТЗ 7.4 defect 1) --------------------------------


def test_warning_never_lands_inside_a_fenced_code_block():
    """Code review: ``QUESTION_ANSWER_BLOCK_SPLIT_RE`` splits on blank
    lines, which a fenced code block may contain -- so the "does this block
    start with a fence" test only recognized the block that opens the
    fence, and a fence with a blank line before its closing delimiter got
    the warning wedged inside the code, where it renders literally instead
    of as a callout."""
    markdown = "```python\nline1\n\nline2\n\n```\n\nОтвет: вот код.\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    before_warning = result.split("> WARN")[0]
    assert before_warning.count("```") % 2 == 0


def test_warning_follows_the_prose_answer_not_a_leading_code_block():
    markdown = "```bash\nls -la\n```\n\nОтвет: команда выше.\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    assert result.index("Ответ: команда выше.") < result.index("> WARN")


def test_never_closed_fence_falls_back_to_putting_the_warning_first():
    """No block outside the fence exists to follow, so the documented
    fallback (warning first) is the only placement that keeps it out of the
    code block."""
    markdown = "```python\nline1\n\nline2\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    assert result.startswith("> WARN")


def test_plain_prose_answer_keeps_the_warning_after_its_first_paragraph():
    markdown = "Ответ по существу.\n\nВторой абзац.\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    assert result == "Ответ по существу.\n\n> WARN\n\nВторой абзац.\n"


def test_prose_opening_an_unclosed_fence_still_keeps_the_warning_outside():
    """Code review: a block may *start* as prose and open a fence further
    down with no blank line between them ("Вот пример:" immediately followed
    by ```python -- a very ordinary model answer). Checking only the block's
    first line called it a real paragraph and put the warning right after
    it, i.e. inside the fence it had just opened."""
    markdown = "Ответ: вот пример.\n```python\ndef f():\n    pass\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    before_warning = result.split("> WARN")[0]
    assert before_warning.count("```") % 2 == 0


def test_prose_then_code_with_nothing_after_it_keeps_the_warning_after_the_prose():
    """Second-round code review: an answer that is *only* an opening sentence
    plus a code block has no later paragraph to fall back to, so skipping the
    block entirely put the warning in front of the answer's real first
    sentence -- exactly the ТЗ 7.4 defect this function exists to prevent.
    Splitting the block keeps the warning both after the prose and outside
    the fence."""
    markdown = (
        "Вот пример:\n```python\ndef foo():\n    pass\n\ndef bar():\n    pass\n```"
    )

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    assert result.startswith("Вот пример:")
    assert result.index("Вот пример:") < result.index("> WARN")
    assert result.split("> WARN")[0].count("```") % 2 == 0


def test_prose_with_a_closed_fence_in_the_same_block_still_gets_the_warning():
    """The mirror case: once the fence the block opened is closed again
    inside that same block, following it is safe -- the fix must not push
    every code-bearing answer onto the warning-first fallback."""
    markdown = "Ответ: вот пример.\n```python\ndef f():\n    pass\n```\n\nДалее.\n"

    result = CliProcessor._insert_after_first_paragraph(markdown, "> WARN")

    assert result.index("Ответ: вот пример.") < result.index("> WARN")
    assert result.index("> WARN") < result.index("Далее.")
