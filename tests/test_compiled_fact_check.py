"""Tests for the monthly compiled fact-check (ТЗ 6.6).

``evaluate_page``/``select_pages_for_fact_check`` are pure and read-only, so
most tests here only assemble a temporary ``compiled/**`` tree. Only the
``run_monthly_fact_check`` orchestration tests need a write path; those
monkeypatch ``patch_validated_vault_frontmatter`` instead of calling it for
real: in this sandbox the real function fails with ``UnsafeVaultPathError:
vault Markdown parent does not exist`` even against a valid manifest and an
existing parent directory (matches the environment limitation already
documented in ``tests/test_compiled_enrich_report.py``), which is why this
module is designed so the write path can be exercised without one.
"""

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import d_brain.run_compiled_monthly_verify as run_compiled_monthly_verify_cli
import d_brain.services.compiled_fact_check as compiled_fact_check
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.compiled_fact_check import (
    CLAIM_CATEGORY_ENVIRONMENT,
    CLAIM_CATEGORY_JUDGMENT,
    CLAIM_CATEGORY_UNVERIFIABLE,
    evaluate_page,
    run_monthly_fact_check,
    select_pages_for_fact_check,
)
from d_brain.services.decisions_queue import QUEUE_CAP

DAY = date(2026, 8, 5)


def _page_text(
    *,
    tier: str = "active",
    last_verified: str = "",
    confidence: str = "high",
    conflicts_open: int = 0,
    sources_rows: list[tuple[str, str, str]],
    conflicts_rows: list[tuple[str, str, str, str, str]] | None = None,
) -> str:
    """Render one ``compiled/**`` page in the shape ``CompiledBriefingService``
    itself writes and parses. The "Sources That Shaped This Page" and "Open
    Conflicts" tables are built with the service's own renderers
    (``_render_sources_shaped_table``/``_render_open_conflicts_table``)
    instead of a hand-rolled duplicate, so a test page is exactly what
    ``compiled_briefings.py`` would produce -- in particular, a wikilink
    never lands inside the claim ("What Added") text, only the Source
    column gets one (mirrors ``_compiled_page_text`` in
    ``tests/test_compiled_enrich_report.py``).
    """
    frontmatter = (
        "---\n"
        "type: compiled-briefing\n"
        "domain: topics\n"
        'description: "Тестовая страница"\n'
        "status: active\n"
        "created: 2026-07-01\n"
        "updated: 2026-07-01\n"
        "freshness_state: fresh\n"
        f"confidence: {confidence}\n"
        f"last_verified: {last_verified}\n"
        f"conflicts_open: {conflicts_open}\n"
        "last_accessed: 2026-07-01\n"
        "relevance: 0.80\n"
        f"tier: {tier}\n"
        "---\n\n"
    )
    body = ["# Тестовая страница", "", "## Sources That Shaped This Page", ""]
    body += CompiledBriefingService._render_sources_shaped_table(sources_rows)
    if conflicts_rows:
        body += ["", "## Open Conflicts", ""]
        body += CompiledBriefingService._render_open_conflicts_table(conflicts_rows)
    return frontmatter + "\n".join(body) + "\n"


def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page_text(**kwargs), encoding="utf-8")  # type: ignore[arg-type]


def _candidate_for(vault: Path, rel_path: str):
    service = CompiledBriefingService(vault)
    for candidate in service._iter_candidates():
        if candidate.rel_path == rel_path:
            return candidate
    raise AssertionError(f"candidate not found: {rel_path}")


# --- claim classification -------------------------------------------------


def test_environment_claim_with_existing_source_passes(
    tmp_path, write_vault_manifest
):
    """The row's Source column -- not the claim text -- is the real signal
    ``compiled_briefings.py`` produces: the compile prompt never asks the
    model to put a wikilink inside the claim itself (code-review defect
    1)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил статус"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_ENVIRONMENT
    assert plan.claims[0].passed is True
    assert plan.failed_count == 0


def test_environment_claim_with_missing_source_fails(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил статус"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_ENVIRONMENT
    assert plan.claims[0].passed is False
    assert plan.failed_count == 1


def test_environment_claim_with_multiple_links_requires_all_to_resolve(
    tmp_path, write_vault_manifest
):
    """A claim naming two documents in its text (on top of its Source) must
    have every one of them resolve, not just one -- regression for a
    mutation that would silently swap "all resolve" for "at least one
    resolves" (code-review defect 7)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            (
                "2026-07-01",
                "daily/2026-07-01.md",
                "Сверено с [[daily/2026-07-01.md]] и с [[daily/2099-01-01.md]]",
            ),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_ENVIRONMENT
    assert plan.claims[0].passed is False
    assert plan.failed_count == 1


def test_environment_claim_source_resolves_through_anchor_prefix_and_no_suffix(
    tmp_path, write_vault_manifest
):
    """The three spellings ``_resolve_vault_wikilink`` accepts on top of a
    plain relative path: a trailing ``#anchor`` (Obsidian's own heading
    link), a leading ``vault/`` (how paths are written in prompts and in the
    daily files themselves), and an extensionless name (``daily/2026-07-01``).
    All three name a file that exists, so treating any of them as broken
    would step the page's confidence down over a link that is in fact fine.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md#встреча", "Ссылка с якорем"),
            ("2026-07-02", "vault/daily/2026-07-01.md", "Ссылка с префиксом vault/"),
            ("2026-07-03", "daily/2026-07-01", "Ссылка без расширения"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert [claim.category for claim in plan.claims] == [CLAIM_CATEGORY_ENVIRONMENT] * 3
    assert [claim.passed for claim in plan.claims] == [True, True, True]
    assert plan.failed_count == 0


def test_environment_claim_source_pointing_outside_the_vault_never_resolves(
    tmp_path, write_vault_manifest
):
    """A ``..`` source must count as unresolved even when the file it names
    really exists next to the vault: the fact-check answers "is this claim
    still backed by the vault", and a path that escapes the vault root is
    not vault evidence."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (tmp_path / "outside.md").write_text("# существует, но снаружи\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "../outside.md", "Клиент подтвердил статус")],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_ENVIRONMENT
    assert plan.claims[0].passed is False


def test_environment_claim_extensionless_source_that_names_nothing_still_fails(
    tmp_path, write_vault_manifest
):
    """The extensionless fallback only tries a ``.md`` sibling -- it must not
    turn every extensionless name into a pass, or a typo in a source would
    be verified forever."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/2026-07-99", "Клиент подтвердил статус")],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_ENVIRONMENT
    assert plan.claims[0].passed is False


def test_task_reference_claim_is_unverifiable_and_excluded_from_checked(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Задача в Todoist закрыта")
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_UNVERIFIABLE
    assert plan.claims[0].passed is None
    assert plan.checked_count == 0
    assert plan.unverifiable_count == 1


def test_task_reference_claim_stays_unverifiable_even_with_resolvable_source(
    tmp_path, write_vault_manifest
):
    """A task-ish claim must stay "unverifiable" even when its Source
    resolves: the Source only proves where the claim came from, not that
    the Todoist task status it describes is still true."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Задача в Todoist закрыта")
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_UNVERIFIABLE
    assert plan.claims[0].passed is None
    assert plan.checked_count == 0


def test_judgment_fallback_reached_only_when_source_column_is_blank(
    tmp_path, write_vault_manifest
):
    """"judgment" is now reached only by a row whose Source is blank -- a
    live table never produces one (``compiled_briefings.py`` always fills
    Source from the excerpt/day file being processed, see
    ``_apply_claims_and_conflicts``), so this exercises the defensive
    fallback for a malformed/legacy row, not a realistic claim."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        conflicts_open=0,
        sources_rows=[("2026-07-01", "   ", "Клиент подтвердил бюджет")],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_JUDGMENT
    assert plan.claims[0].passed is True


def test_judgment_claim_fails_when_conflicts_open_counter_is_nonzero(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        conflicts_open=1,
        sources_rows=[("2026-07-01", "   ", "Клиент подтвердил бюджет")],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_JUDGMENT
    assert plan.claims[0].passed is False


def test_judgment_claim_fails_when_open_conflicts_rows_present_despite_zero_counter(
    tmp_path, write_vault_manifest,
):
    """ТЗ 6.6 mentions both ``conflicts_open`` and ``_open_conflicts_rows`` as
    the signal -- a stale/desynced counter must not hide a real open row."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        conflicts_open=0,
        sources_rows=[("2026-07-01", "   ", "Клиент подтвердил бюджет")],
        conflicts_rows=[
            (
                "2026-07-15",
                "старое",
                "daily/2026-07-10.md",
                "новое",
                "daily/2026-07-15.md",
            )
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.claims[0].category == CLAIM_CATEGORY_JUDGMENT
    assert plan.claims[0].passed is False


# --- half-threshold gate ---------------------------------------------------


def test_page_advances_last_verified_when_exactly_half_claims_fail(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-01-01.md", "Клиент подтвердил B"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.checked_count == 2
    assert plan.failed_count == 1
    assert plan.exceeded_half is False
    assert plan.next_last_verified == DAY.isoformat()


def test_page_does_not_advance_when_more_than_half_claims_fail(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-01-01.md", "Клиент подтвердил B"),
            ("2026-07-03", "daily/2099-02-02.md", "Клиент подтвердил C"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.checked_count == 3
    assert plan.failed_count == 2
    assert plan.exceeded_half is True
    assert plan.next_last_verified == "2026-06-01"
    assert "last_verified" not in plan.frontmatter_updates


def test_page_with_no_checked_claims_does_not_advance_last_verified(
    tmp_path, write_vault_manifest
):
    """"Nothing was checked" must not look like "everything passed"
    (code-review defect 6): a page whose only claims are unverifiable must
    not be treated as freshly verified today."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Задача в Todoist закрыта"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.checked_count == 0
    assert plan.next_last_verified == "2026-06-01"
    assert "last_verified" not in plan.frontmatter_updates


# --- confidence cap / step-down --------------------------------------------


def test_confidence_steps_down_one_level_on_gate_failure(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.exceeded_half is True
    assert plan.unverifiable_count == 0
    assert plan.next_confidence == "medium"


def test_confidence_caps_at_medium_when_unverifiable_claim_present(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2026-07-02.md", "Задача в Todoist закрыта"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.exceeded_half is False
    assert plan.unverifiable_count == 1
    assert plan.next_confidence == "medium"


def test_confidence_effects_combine_without_double_stepping(
    tmp_path, write_vault_manifest
):
    """ТЗ 6.6: both effects can apply to the same page. From "high" they
    both land on "medium" (one is a floor, the other a one-step drop from
    "high"), so this asserts the combination stops there and does not fall
    through to "low"."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
            ("2026-07-03", "daily/2026-07-03.md", "Задача в Todoist закрыта"),
        ],
    )

    plan = evaluate_page(
        vault, _candidate_for(vault, "compiled/topics/aurora.md"), today=DAY
    )

    assert plan.exceeded_half is True
    assert plan.unverifiable_count == 1
    assert plan.next_confidence == "medium"


# --- page selection ---------------------------------------------------------


def test_selection_filters_by_eligible_tier(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    for tier in ("core", "active", "warm", "cold"):
        _write_page(
            vault,
            f"compiled/topics/{tier}.md",
            tier=tier,
            sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
        )

    selected = select_pages_for_fact_check(CompiledBriefingService(vault), limit=10)

    assert {candidate.rel_path for candidate in selected} == {
        "compiled/topics/core.md",
        "compiled/topics/active.md",
        "compiled/topics/warm.md",
    }


def test_selection_includes_pages_with_empty_tier(tmp_path, write_vault_manifest):
    """A page the memory-decay engine has not scored yet (empty tier) must
    not be silently excluded forever (code-review defect 5)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/unscored.md",
        tier="",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
    )

    selected = select_pages_for_fact_check(CompiledBriefingService(vault), limit=10)

    assert {candidate.rel_path for candidate in selected} == {
        "compiled/topics/unscored.md"
    }


def test_selection_orders_empty_last_verified_first_then_oldest(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/newer.md",
        last_verified="2026-07-20",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
    )
    _write_page(
        vault,
        "compiled/topics/older.md",
        last_verified="2026-06-01",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
    )
    _write_page(
        vault,
        "compiled/topics/never.md",
        last_verified="",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
    )

    selected = select_pages_for_fact_check(CompiledBriefingService(vault), limit=10)

    assert [candidate.rel_path for candidate in selected] == [
        "compiled/topics/never.md",
        "compiled/topics/older.md",
        "compiled/topics/newer.md",
    ]


def test_selection_respects_page_limit(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    for index in range(5):
        _write_page(
            vault,
            f"compiled/topics/page-{index}.md",
            sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
        )

    selected = select_pages_for_fact_check(CompiledBriefingService(vault), limit=2)

    assert len(selected) == 2


def test_selection_excludes_archive_domain_even_with_eligible_tier(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/archive/old.md",
        tier="core",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "заметка")],
    )

    selected = select_pages_for_fact_check(CompiledBriefingService(vault), limit=10)

    assert selected == []


# Decisions-queue appender tests moved to tests/test_decisions_queue.py
# (задача L): the function itself now lives in
# d_brain.services.decisions_queue as the shared writer for any producer,
# with the ТЗ 7.2 30-item cap added.


# --- run_monthly_fact_check orchestration -----------------------------------


def test_run_monthly_fact_check_advances_and_patches_only_last_verified(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-01.md").write_text("# ok\n", encoding="utf-8")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил статус"),
        ],
    )
    calls: list[tuple[Path, dict[str, str]]] = []

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        calls.append((path, dict(updates)))

    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", _fake_patch
    )

    result = run_monthly_fact_check(vault, today=DAY)

    assert result["status"] == "ok"
    assert result["pages_checked"] == 1
    assert result["pages_patched"] == 1
    assert result["pages_flagged"] == 0
    assert len(calls) == 1
    patched_path, updates = calls[0]
    assert patched_path == vault / "compiled/topics/aurora.md"
    assert updates == {"last_verified": DAY.isoformat()}


def test_run_monthly_fact_check_queues_and_holds_when_gate_fails(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        compiled_fact_check,
        "patch_validated_vault_frontmatter",
        lambda vault_path, path, updates, *, manifest=None, existing_lock=None: (
            calls.append(dict(updates))
        ),
    )

    result = run_monthly_fact_check(vault, today=DAY)

    assert result["pages_patched"] == 1
    assert result["pages_flagged"] == 1
    # last_verified is untouched (gate failed): only confidence stepped down.
    assert calls == [{"confidence": "medium"}]

    queue_path = vault / ".session" / "decisions-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 1
    assert queue[0]["kind"] == "fact-check-rejected"
    assert queue[0]["page"] == "compiled/topics/aurora.md"


def test_run_monthly_fact_check_still_queues_page_when_frontmatter_patch_fails(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Queuing must not depend on the frontmatter write succeeding
    (code-review defect 2). Reproduced by making the patch raise for a
    page with two failed claims out of two: before the fix this produced
    zero flagged pages and no decisions-queue file, even though the error
    itself was reported."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )

    def _boom(vault_path, path, updates, *, manifest=None, existing_lock=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(compiled_fact_check, "patch_validated_vault_frontmatter", _boom)

    result = run_monthly_fact_check(vault, today=DAY)

    assert result["pages_patched"] == 0
    assert result["pages_flagged"] == 1
    assert result["errors"] == ["compiled/topics/aurora.md: boom"]

    queue_path = vault / ".session" / "decisions-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 1
    assert queue[0]["page"] == "compiled/topics/aurora.md"


def test_run_monthly_fact_check_does_not_grow_queue_for_repeatedly_failing_page(
    tmp_path, write_vault_manifest, monkeypatch
):
    """A page that keeps failing every run must not add a new
    decisions-queue row every run (code-review defect 4)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )

    run_monthly_fact_check(vault, today=DAY)
    run_monthly_fact_check(vault, today=date(2026, 9, 5))

    queue_path = vault / ".session" / "decisions-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 1


def test_run_monthly_fact_check_writes_own_journal_separate_from_compile_enrich(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "Клиент подтвердил A")],
    )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )

    run_monthly_fact_check(vault, today=DAY)

    journal_path = vault / ".session" / "compile-fact-check.json"
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    assert payload["date"] == DAY.isoformat()
    assert payload["pages_checked"] == ["compiled/topics/aurora.md"]
    assert not (vault / ".session" / "compile-enrich.json").exists()


def test_run_monthly_fact_check_journals_even_when_the_queue_write_blows_up(
    tmp_path, write_vault_manifest, monkeypatch
):
    """The frontmatter patches are already on disk by the time
    ``append_decision_queue_entries`` runs, and this journal is the only
    place the digest learns what this run patched, flagged, and evicted.
    Letting the exception skip it left those writes done but unreported --
    the same reason ``compiled_briefings.py``'s nightly pass journals in a
    ``finally``."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("decisions queue file corrupt")

    monkeypatch.setattr(compiled_fact_check, "append_decision_queue_entries", _boom)

    with pytest.raises(RuntimeError, match="decisions queue file corrupt"):
        run_monthly_fact_check(vault, today=DAY)

    journal = json.loads(
        (vault / ".session" / "compile-fact-check.json").read_text(encoding="utf-8")
    )
    assert journal["pages_checked"] == ["compiled/topics/aurora.md"]
    assert journal["pages_flagged"] == ["compiled/topics/aurora.md"]


def test_a_failing_journal_write_does_not_replace_the_real_cause(
    tmp_path, write_vault_manifest, monkeypatch, caplog
):
    """The journal is written from ``finally``, and an exception raised
    there *replaces* whatever is already propagating -- the real cause
    survives only in ``__context__``, which no caller reads. A full disk at
    that exact moment must not rename someone else's failure."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A")],
    )

    def _boom_manifest(*args, **kwargs):
        raise RuntimeError("настоящая причина: манифест не читается")

    def _boom_journal(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(compiled_fact_check, "load_manifest_for_vault", _boom_manifest)
    monkeypatch.setattr(compiled_fact_check, "_write_fact_check_journal", _boom_journal)

    with caplog.at_level("WARNING", logger="d_brain.services.compiled_fact_check"):
        with pytest.raises(RuntimeError, match="манифест не читается"):
            run_monthly_fact_check(vault, today=DAY)

    assert "Failed to write monthly fact-check journal" in caplog.text


def test_run_monthly_fact_check_reports_queue_evictions_when_batch_exceeds_cap(
    tmp_path, write_vault_manifest, monkeypatch
):
    """A batch of failing pages larger than ``QUEUE_CAP`` forces
    ``append_decision_queue_entries`` to evict some of its own entries
    (``decisions_queue._trim_new_entries_to_cap``) -- the exact "bulk
    producer" scenario that module's docstring names this call out as. The
    eviction count must not be silently dropped: it belongs in the returned
    ``result`` (surfaced by the CLI's printed JSON) and in this run's own
    journal, since this path has no active ``CompileEnrichPass`` to fold it
    into the way ``compiled_briefings.py``'s callers do."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    page_count = QUEUE_CAP + 2
    for i in range(page_count):
        _write_page(
            vault,
            f"compiled/topics/aurora-{i:02d}.md",
            last_verified="2026-06-01",
            confidence="high",
            sources_rows=[
                ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
                ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
            ],
        )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )

    result = run_monthly_fact_check(vault, today=DAY, page_limit=page_count)

    assert result["pages_flagged"] == page_count
    assert result["queue_evictions"] == 2

    queue_path = vault / ".session" / "decisions-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == QUEUE_CAP

    journal_path = vault / ".session" / "compile-fact-check.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["queue_evictions"] == 2


def test_run_monthly_fact_check_reports_zero_queue_evictions_for_small_batch(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Regression: an ordinary small batch (well under ``QUEUE_CAP``) must
    not report any eviction."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "Клиент подтвердил A"),
            ("2026-07-02", "daily/2099-02-02.md", "Клиент подтвердил B"),
        ],
    )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )

    result = run_monthly_fact_check(vault, today=DAY)

    assert result["pages_flagged"] == 1
    assert result["queue_evictions"] == 0

    journal_path = vault / ".session" / "compile-fact-check.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["queue_evictions"] == 0


def test_run_monthly_fact_check_with_no_eligible_pages_is_no_work(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    (vault / "compiled").mkdir(parents=True)
    write_vault_manifest(vault)

    result = run_monthly_fact_check(vault, today=DAY)

    assert result["status"] == "no-work"
    assert result["pages_checked"] == 0
    assert not (vault / ".session" / "decisions-queue.json").exists()
    journal_path = vault / ".session" / "compile-fact-check.json"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["pages_checked"] == []


# --- CLI entrypoint ----------------------------------------------------------


def test_cli_main_prints_result_json_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "run_monthly_fact_check",
        lambda vault_path, *, page_limit=20: {
            "status": "no-work",
            "pages_checked": 0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )
    monkeypatch.setattr(sys, "argv", ["run_compiled_monthly_verify.py"])

    exit_code = run_compiled_monthly_verify_cli.main()

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "no-work"


def test_cli_main_passes_page_limit_argument_through(tmp_path, monkeypatch, capsys):
    received: dict[str, int] = {}

    def _fake_run_monthly_fact_check(vault_path, *, page_limit=20):
        received["page_limit"] = page_limit
        return {"status": "no-work", "pages_checked": 0, "errors": []}

    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "run_monthly_fact_check",
        _fake_run_monthly_fact_check,
    )
    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )
    monkeypatch.setattr(
        sys, "argv", ["run_compiled_monthly_verify.py", "--page-limit", "7"]
    )

    exit_code = run_compiled_monthly_verify_cli.main()

    assert exit_code == 0
    assert received["page_limit"] == 7


def test_cli_main_returns_nonzero_when_errors_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "run_monthly_fact_check",
        lambda vault_path, *, page_limit=20: {
            "status": "ok",
            "errors": ["compiled/topics/aurora.md: boom"],
        },
    )
    monkeypatch.setattr(
        run_compiled_monthly_verify_cli,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )
    monkeypatch.setattr(sys, "argv", ["run_compiled_monthly_verify.py"])

    exit_code = run_compiled_monthly_verify_cli.main()

    assert exit_code == 1
