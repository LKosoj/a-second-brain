"""Tests for the owner decisions queue (задача L, ТЗ 7.2).

``list_queue_items``/``action_button_specs`` are pure and read-only, so most
tests here only assemble a temporary ``compiled/**`` tree and/or
``.session/decisions-queue.json``. ``apply_response`` tests whose page write
must actually *succeed* monkeypatch ``patch_validated_vault_frontmatter``/
``write_validated_vault_markdown`` instead of calling them for real: in this
sandbox the real functions fail with ``UnsafeVaultPathError: vault Markdown
parent does not exist`` even against a valid manifest and an existing parent
directory (same environment limitation documented in
``tests/test_compiled_fact_check.py``/``tests/test_compiled_enrich_report.py``),
confirmed independently for this task -- see the final report. Plain reads
(``read_vault_file_bytes``) and the JSON queue file's tempfile+os.replace
writer (``_atomic_write_text``) are not fd-pinned the same way and work for
real, so those are exercised directly. A few tests (code review defects 1-3)
deliberately call the real, unmonkeypatched write/patch/read helpers *because*
they need the real failure path -- a missing page, or a lock-reentrancy check
that only means anything against the real ``vault_write_lock``.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest

from d_brain.manifest import load_manifest_for_vault
from d_brain.services import compiled_fact_check, decisions_queue
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.compiled_fact_check import run_monthly_fact_check
from d_brain.services.decisions_queue import (
    BLOCKED_ACTION_KIND,
    CONFLICT_KIND,
    DRIFT_KIND,
    DUPLICATE_CANDIDATE_KIND,
    FACT_CHECK_REJECTED_KIND,
    HUMAN_ZONE_AMBIGUOUS_KIND,
    PAGE_ENCODING_BROKEN_KIND,
    QUEUE_CAP,
    VERIFY_REJECTED_KIND,
    QueueItem,
    ResponseOutcome,
    action_button_specs,
    append_decision_queue_entries,
    apply_response,
    format_queue_item_line,
    list_queue_items,
    mark_page_human_reviewed,
    queue_document_path,
    queue_item_fingerprint,
    queue_kind_label,
    render_queue_document,
    write_queue_document,
)

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
    itself writes and parses, using its own table renderers -- mirrors
    ``tests/test_compiled_fact_check.py``'s ``_page_text`` exactly, so a
    round trip through ``_open_conflicts_rows``/``_render_open_conflicts_table``
    behaves exactly as it would in production.
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


def _write_queue(vault: Path, entries: list[dict[str, object]]) -> None:
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "decisions-queue.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


def _sources_table_section(text: str) -> str:
    return CompiledBriefingService._section_text(text, "Sources That Shaped This Page")


# --- list_queue_items --------------------------------------------------------


def test_list_queue_items_includes_json_backed_fact_check_entry(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "проверка фактов: 2/3 не подтвердились",
                "since": "2026-07-01",
            }
        ],
    )

    items = list_queue_items(vault)

    assert items == [
        QueueItem(
            kind="fact-check-rejected",
            page="compiled/topics/aurora.md",
            summary="проверка фактов: 2/3 не подтвердились",
            since="2026-07-01",
        )
    ]


def test_list_queue_items_includes_conflict_rows_from_pages(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт продлён на год",
        "daily/2026-07-01.md",
        "Контракт продлён на два года",
        "daily/2026-07-15.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/2026-07-01.md", "Контракт продлён")],
        conflicts_rows=[row],
        conflicts_open=1,
    )

    items = list_queue_items(vault)

    assert len(items) == 1
    assert items[0].kind == CONFLICT_KIND
    assert items[0].page == "compiled/topics/aurora.md"
    assert items[0].conflict_row == row
    assert "Контракт продлён на год" in items[0].summary
    assert "Контракт продлён на два года" in items[0].summary


def test_list_queue_items_combines_json_then_conflicts_in_order(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    row = ("2026-07-15", "A", "daily/a.md", "B", "daily/b.md")
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )

    items = list_queue_items(vault)

    assert [item.kind for item in items] == [FACT_CHECK_REJECTED_KIND, CONFLICT_KIND]


def test_list_queue_items_missing_json_file_yields_only_conflicts(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)

    assert list_queue_items(vault) == []


def test_list_queue_items_preserves_unknown_kind(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "duplicate-candidate",
                "page": "compiled/topics/aurora.md",
                "summary": "похожая страница уже существует",
                "since": "2026-07-01",
            }
        ],
    )

    items = list_queue_items(vault)

    assert items[0].kind == "duplicate-candidate"


# --- action_button_specs -----------------------------------------------------


def test_action_button_specs_fact_check_rejected():
    item = QueueItem(
        kind=FACT_CHECK_REJECTED_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    ids = [action_id for action_id, _label in action_button_specs(item)]

    assert ids == ["confirm", "reject", "defer"]


def test_action_button_specs_conflict_shows_claim_text_not_abstract_labels():
    row = (
        "2026-07-15",
        "Контракт продлён на год",
        "a.md",
        "Контракт продлён на два года",
        "b.md",
    )
    item = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-15", conflict_row=row,
    )

    specs = action_button_specs(item)

    labels = [label for _action_id, label in specs]
    assert "Контракт продлён на год" in labels[0]
    assert "Контракт продлён на два года" in labels[1]
    assert specs[2] == ("defer", "Отложить")


def test_action_button_specs_unknown_kind_only_reject_and_defer():
    item = QueueItem(
        kind="drift", page="compiled/topics/aurora.md", summary="s", since=""
    )

    ids = [action_id for action_id, _label in action_button_specs(item)]

    assert ids == ["reject", "defer"]
    assert "confirm" not in ids


# --- append_decision_queue_entries (moved from test_compiled_fact_check.py,
# задача L, with the ТЗ 7.2 30-item cap added) --------------------------------


def test_append_decision_queue_creates_file_when_missing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    entry = {
        "kind": "fact-check-rejected",
        "page": "compiled/topics/aurora.md",
        "summary": "s",
        "since": "2026-08-05",
    }

    append_decision_queue_entries(vault, [entry])

    queue_path = vault / ".session" / "decisions-queue.json"
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    assert payload == [entry]


def test_append_decision_queue_appends_to_existing_list(tmp_path):
    vault = tmp_path / "vault"
    _write_queue(
        vault,
        [
            {
                "kind": "conflict",
                "page": "compiled/topics/other.md",
                "summary": "старое",
                "since": "2026-07-01",
            }
        ],
    )

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "новое",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(payload) == 2
    assert payload[0]["page"] == "compiled/topics/other.md"
    assert payload[1]["page"] == "compiled/topics/aurora.md"


def test_append_decision_queue_tolerates_corrupt_existing_file(tmp_path):
    vault = tmp_path / "vault"
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "decisions-queue.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (session_dir / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(payload) == 1


def test_append_decision_queue_skips_duplicate_open_entry(tmp_path):
    vault = tmp_path / "vault"
    existing_entry = {
        "kind": "fact-check-rejected",
        "page": "compiled/topics/aurora.md",
        "summary": "проверка фактов: 1/2 не подтвердились",
        "since": "2026-07-01",
    }
    _write_queue(vault, [existing_entry])

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "проверка фактов: 2/3 не подтвердились",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert payload == [existing_entry]


def test_append_decision_queue_entries_dedups_within_same_call(tmp_path):
    """Defect (minor, code review): dedup only checked entries already on
    file, not entries against each other within the *same* call -- today
    every real producer passes one item at a time so this never triggered,
    but this module is the shared writer for any current or future
    producer, and a caller passing several entries in one call is a
    supported use, not a misuse."""
    vault = tmp_path / "vault"

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "первая",
                "since": "2026-08-01",
            },
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "вторая",
                "since": "2026-08-05",
            },
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(payload) == 1
    assert payload[0]["summary"] == "первая"


def test_append_decision_queue_evicts_oldest_nonprotected_entry_on_overflow(tmp_path):
    """ТЗ 5.6 'Размер очереди решений: 30' -- adding a 31st entry must evict
    the single oldest (by ``since``) entry, not just silently grow past cap."""
    vault = tmp_path / "vault"
    existing = [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": f"2026-01-{i + 1:02d}",
        }
        for i in range(30)
    ]
    _write_queue(vault, existing)

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/new-page.md",
                "summary": "s",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(payload) == 30
    pages = {entry["page"] for entry in payload}
    assert "compiled/topics/page-0.md" not in pages  # oldest since -- evicted
    assert "compiled/topics/new-page.md" in pages


def test_append_decision_queue_never_evicts_protected_kind(tmp_path):
    """A ``conflict``-kind entry (never actually produced today, but
    protected by kind per the module docstring) must survive overflow even
    though it is the oldest entry on file."""
    vault = tmp_path / "vault"
    protected = {
        "kind": "conflict",
        "page": "compiled/topics/protected.md",
        "summary": "s",
        "since": "2020-01-01",
    }
    existing = [protected] + [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": f"2026-01-{i + 1:02d}",
        }
        for i in range(29)
    ]
    _write_queue(vault, existing)

    append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/new-page.md",
                "summary": "s",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    pages = {entry["page"] for entry in payload}
    assert "compiled/topics/protected.md" in pages
    assert "compiled/topics/page-0.md" not in pages  # oldest evictable entry


def test_append_decision_queue_entries_returns_evicted_count(tmp_path):
    """Code review (ТЗ 7.2): the caller must learn *how many* entries an
    overflow evicted so it can fold that into the pass journal/digest --
    previously ``append_decision_queue_entries`` returned ``None`` and the
    eviction fact never reached the owner at all."""
    vault = tmp_path / "vault"

    no_overflow = append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/first.md",
                "summary": "s",
                "since": "2026-08-01",
            }
        ],
    )
    assert no_overflow == 0

    same_call_again = append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/first.md",
                "summary": "s",
                "since": "2026-08-01",
            }
        ],
    )
    assert same_call_again == 0  # nothing new appended (dedup) -> nothing evicted

    existing = [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": f"2026-01-{i + 1:02d}",
        }
        for i in range(29)
    ]
    _write_queue(vault, existing)

    evicted = append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/new-a.md",
                "summary": "s",
                "since": "2026-08-05",
            },
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/new-b.md",
                "summary": "s",
                "since": "2026-08-05",
            },
        ],
    )
    # 29 existing + 2 new = 31, one over QUEUE_CAP (30) -> exactly one evicted.
    assert evicted == 1


def test_append_decision_queue_never_evicts_new_entries_when_protected_backlog_at_cap(
    tmp_path,
):
    """Code review (ТЗ 7.2): once protected-kind entries (``conflict``,
    ``blocked-action``) alone already fill the queue to ``QUEUE_CAP``, a
    brand-new non-protected entry added in the *same* call used to be the
    only evictable candidate in the combined list and got dropped before
    the owner ever saw it -- the starvation bug this fix closes. The
    combined list is allowed to grow past cap in this pathological case;
    losing a fresh item silently is the worse outcome."""
    vault = tmp_path / "vault"
    protected_backlog = [
        {
            "kind": BLOCKED_ACTION_KIND,
            "page": f"compiled/topics/blocked-{i}.md",
            "summary": "s",
            "since": f"2026-01-{i + 1:02d}",
        }
        for i in range(decisions_queue.QUEUE_CAP)
    ]
    _write_queue(vault, protected_backlog)

    evicted = append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/fresh.md",
                "summary": "s",
                "since": "2026-08-05",
            }
        ],
    )

    assert evicted == 0
    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    pages = {entry["page"] for entry in payload}
    # All 30 protected entries survive AND the new one is present -- 31
    # total, over cap, rather than the new entry being silently dropped.
    assert len(payload) == decisions_queue.QUEUE_CAP + 1
    assert "compiled/topics/fresh.md" in pages
    assert all(f"compiled/topics/blocked-{i}.md" in pages for i in range(30))


def test_append_decision_queue_batch_larger_than_cap_on_empty_queue_is_trimmed(
    tmp_path,
):
    """Code review: a single call's own ``new_entries`` batch can exceed
    ``QUEUE_CAP`` all by itself -- e.g. ``compiled_fact_check.py`` driven by
    ``run_compiled_monthly_verify.py --page-limit`` above 30 -- even when
    the queue file starts out empty. ``_evict_overflow`` used to only ever
    look at ``existing`` for eviction candidates, so an oversized batch on
    an empty (or small) queue landed on disk untouched, blowing straight
    past ``QUEUE_CAP``."""
    vault = tmp_path / "vault"
    base = date(2026, 1, 1)
    entries = [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": (base + timedelta(days=i)).isoformat(),
        }
        for i in range(45)
    ]

    evicted = append_decision_queue_entries(vault, entries)

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 15
    assert len(payload) == decisions_queue.QUEUE_CAP
    pages = {entry["page"] for entry in payload}
    # Oldest 15 (by since) dropped, newest 30 kept.
    assert pages == {f"compiled/topics/page-{i}.md" for i in range(15, 45)}


def test_append_decision_queue_protected_batch_larger_than_cap_is_allowed(tmp_path):
    """The protected-entries overflow exemption (module docstring:
    ``blocked-action``/``conflict`` may push the queue past ``QUEUE_CAP``
    rather than being evicted) applies just as much when the oversized
    batch itself is entirely protected, not only when an ``existing``
    protected backlog is at fault -- the fix must not start evicting
    protected entries just because they arrived in a big batch."""
    vault = tmp_path / "vault"
    entries = [
        {
            "kind": BLOCKED_ACTION_KIND,
            "page": f"compiled/topics/blocked-{i}.md",
            "summary": "s",
            "since": "2026-08-05",
        }
        for i in range(35)
    ]

    evicted = append_decision_queue_entries(vault, entries)

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 0
    assert len(payload) == 35  # over QUEUE_CAP, allowed -- all protected
    pages = {entry["page"] for entry in payload}
    assert pages == {f"compiled/topics/blocked-{i}.md" for i in range(35)}


def test_append_decision_queue_mixed_batch_larger_than_cap_trims_only_nonprotected(
    tmp_path,
):
    """A single oversized batch mixing protected and non-protected kinds:
    every protected entry survives, only the non-protected ones are
    trimmed (oldest by ``since`` first) down to fit ``QUEUE_CAP``."""
    vault = tmp_path / "vault"
    base = date(2026, 1, 1)
    protected_entries = [
        {
            "kind": BLOCKED_ACTION_KIND,
            "page": f"compiled/topics/blocked-{i}.md",
            "summary": "s",
            "since": "2026-08-05",
        }
        for i in range(5)
    ]
    nonprotected_entries = [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": (base + timedelta(days=i)).isoformat(),
        }
        for i in range(30)
    ]
    entries = protected_entries + nonprotected_entries  # 35 total, 5 over cap

    evicted = append_decision_queue_entries(vault, entries)

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 5
    assert len(payload) == decisions_queue.QUEUE_CAP
    pages = {entry["page"] for entry in payload}
    assert pages == (
        {f"compiled/topics/blocked-{i}.md" for i in range(5)}
        | {f"compiled/topics/page-{i}.md" for i in range(5, 30)}
    )


def test_append_decision_queue_drift_supersession_ignores_a_trimmed_batch_entry(
    tmp_path,
):
    """A fresh drift entry may only retire that page's stale one if it
    actually survives into the file.

    Supersession used to be computed from the untrimmed batch, so a fresh
    drift entry that ``_trim_new_entries_to_cap`` then dropped still
    retired the old one on its way out -- the page's drift signal was
    removed on behalf of an entry nobody would ever see, and that removal
    was not even counted as an eviction. The count is what makes it
    visible: two entries really do leave here (the batch's own trimmed
    drift, and the stale one the full queue pushes out), so the caller
    must be told about two, not one."""
    vault = tmp_path / "vault"
    _write_queue(
        vault,
        [
            {
                "kind": DRIFT_KIND,
                "page": "compiled/topics/drifting.md",
                "summary": "старый дрейф",
                "since": "2026-01-15",
            }
        ],
    )
    base = date(2026, 3, 1)
    # A different month than the stale entry, so dedup lets it through, and
    # the oldest ``since`` in the batch, so trimming picks it first.
    batch = [
        {
            "kind": DRIFT_KIND,
            "page": "compiled/topics/drifting.md",
            "summary": "свежий дрейф",
            "since": "2026-02-01",
        }
    ]
    batch += [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/page-{i}.md",
            "summary": "s",
            "since": (base + timedelta(days=i)).isoformat(),
        }
        for i in range(decisions_queue.QUEUE_CAP)
    ]

    evicted = append_decision_queue_entries(vault, batch)

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 2
    assert len(payload) == decisions_queue.QUEUE_CAP
    assert "свежий дрейф" not in {entry["summary"] for entry in payload}


@pytest.mark.parametrize("protected_count", [decisions_queue.QUEUE_CAP, 32])
def test_append_decision_queue_mixed_batch_keeps_new_kinds_when_protected_fill_cap(
    tmp_path, protected_count
):
    """The protected entries of one batch already filling ``QUEUE_CAP`` must
    not cost that batch every non-protected entry it also carried.

    Trimming keeps the newest non-protected entries and drops the oldest.
    With no room left at all, "keep the newest" degenerates into "keep
    none": the freshest signals in the batch are dropped in full and
    silently, which is worse than the cap being exceeded. Both docstrings
    promise the lesser evil here -- the batch passes through untouched,
    exactly as the ``existing`` side already behaves when its own protected
    backlog overflows.

    Both sides of that "no room left" boundary are checked: protected
    entries filling the cap exactly, and overfilling it. Only the exact
    case pins the comparison down -- with just the overfilled one, a
    later ``room <= 0`` slipping to ``room < 0`` would still pass."""
    vault = tmp_path / "vault"
    protected_entries = [
        {
            "kind": BLOCKED_ACTION_KIND,
            "page": f"compiled/topics/blocked-{i}.md",
            "summary": "s",
            "since": "2026-08-05",
        }
        for i in range(protected_count)
    ]
    fresh_entries = [
        {
            "kind": "page-encoding-broken",
            "page": f"compiled/topics/broken-{i}.md",
            "summary": "s",
            "since": "2026-08-06",
        }
        for i in range(3)
    ]

    evicted = append_decision_queue_entries(vault, protected_entries + fresh_entries)

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 0
    assert len(payload) == protected_count + 3
    pages = {entry["page"] for entry in payload}
    assert {f"compiled/topics/broken-{i}.md" for i in range(3)} <= pages


def test_append_decision_queue_small_batch_no_overflow_unaffected(tmp_path):
    """Regression: a normal, small batch that does not overflow the cap is
    completely unaffected by the batch-trimming fix -- nothing evicted,
    every existing and new entry kept."""
    vault = tmp_path / "vault"
    existing = [
        {
            "kind": "fact-check-rejected",
            "page": f"compiled/topics/old-{i}.md",
            "summary": "s",
            "since": f"2026-01-{i + 1:02d}",
        }
        for i in range(5)
    ]
    _write_queue(vault, existing)

    evicted = append_decision_queue_entries(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/new.md",
                "summary": "s",
                "since": "2026-08-05",
            }
        ],
    )

    payload = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert evicted == 0
    assert len(payload) == 6
    pages = {entry["page"] for entry in payload}
    assert pages == {f"compiled/topics/old-{i}.md" for i in range(5)} | {
        "compiled/topics/new.md"
    }


# --- append_decision_queue_entries: mirror regeneration, no self-deadlock ---
#
# ТЗ 7.2 code review: the human-readable mirror must be regenerated "at
# every queue change", not just when the owner responds or the nightly
# safety net runs. The danger is a caller that already holds
# ``vault_write_lock`` when it calls ``append_decision_queue_entries``: flock
# is not reentrant across two open file descriptors to the same file, so
# regeneration must reuse that already-held lock, passed through explicitly
# as ``existing_lock``, rather than open a second one -- opening a second
# one here has actually hung this codebase before. An earlier version
# rediscovered the ambient lock itself via a process-wide registry
# (``find_live_vault_write_lock``); that was replaced (code review defect)
# because the registry could not tell *this* call stack's own lock apart
# from another thread's unrelated live lock for the same path, and iterating
# it while another thread's lock context opened or closed could raise
# ``RuntimeError: Set changed size during iteration``. Each test below runs
# the real call on a background thread and asserts the thread finished
# within a bounded ``join(timeout=...)`` -- a broken implementation that
# opens a fresh lock instead of using the passed-in one would leave the
# thread permanently blocked on ``flock()``, which a plain "was the function
# called" assertion would never catch.


def _drift_entry(page: str, since: str) -> dict[str, str]:
    return {
        "kind": "drift",
        "page": page,
        "summary": f"страница обогащалась слишком часто ({since})",
        "since": since,
    }


def test_drift_entry_from_a_later_month_replaces_the_stale_one(
    tmp_path, write_vault_manifest, monkeypatch
):
    """ТЗ 5.6 counts enrichments per calendar month, so drifting again in
    August is a new event -- keying it only on ``(kind, page)`` let July's
    unanswered entry swallow it, leaving the owner with a stale date and
    summary (found in review)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    page = "compiled/topics/aurora.md"
    with decisions_queue.vault_write_lock(vault) as lock:
        append_decision_queue_entries(
            vault, [_drift_entry(page, "2026-07-10")], existing_lock=lock
        )
        append_decision_queue_entries(
            vault, [_drift_entry(page, "2026-07-28")], existing_lock=lock
        )
        append_decision_queue_entries(
            vault, [_drift_entry(page, "2026-08-04")], existing_lock=lock
        )

    queued = json.loads(
        (vault / decisions_queue.DECISIONS_QUEUE_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    drift = [item for item in queued if item["kind"] == "drift"]
    assert len(drift) == 1, "one current drift signal per page, not one per month"
    assert drift[0]["since"] == "2026-08-04"
    assert "2026-08-04" in drift[0]["summary"]


def test_two_drift_entries_from_one_month_in_one_call_collapse_to_one(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Within a single call, the month is what makes two drift entries for
    one page the same event.

    The test above proves a *later* month is a new event; this one holds
    the other half down. Dedup inside a call is the only thing collapsing
    these -- supersession only reaches entries already on file, so if the
    key ever narrowed to the day, both would land and the page would carry
    two live drift signals for the same month at once."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    page = "compiled/topics/aurora.md"
    with decisions_queue.vault_write_lock(vault) as lock:
        append_decision_queue_entries(
            vault,
            [_drift_entry(page, "2026-07-10"), _drift_entry(page, "2026-07-28")],
            existing_lock=lock,
        )

    queued = json.loads(
        (vault / decisions_queue.DECISIONS_QUEUE_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    drift = [item for item in queued if item["kind"] == "drift"]
    assert len(drift) == 1
    assert drift[0]["since"] == "2026-07-10"


def test_a_fresh_drift_entry_only_retires_that_page_s_drift_entry(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Supersession is the one place a queue entry is dropped without being
    counted as an eviction, so it has to reach exactly one entry: that
    page's own stale drift.

    A page can hold several open decisions at once, each a different kind
    and each answered separately. Retiring by page alone would let a new
    drift signal silently take the page's ``verify-rejected`` with it --
    no count, no digest line, a decision the owner is simply never asked
    to make again."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    page = "compiled/topics/aurora.md"
    with decisions_queue.vault_write_lock(vault) as lock:
        append_decision_queue_entries(
            vault,
            [
                _drift_entry(page, "2026-07-10"),
                {
                    "kind": VERIFY_REJECTED_KIND,
                    "page": page,
                    "summary": "проверка отклонила заявления",
                    "since": "2026-07-11",
                },
            ],
            existing_lock=lock,
        )
        append_decision_queue_entries(
            vault, [_drift_entry(page, "2026-08-04")], existing_lock=lock
        )

    queued = json.loads(
        (vault / decisions_queue.DECISIONS_QUEUE_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    by_kind = {item["kind"]: item for item in queued}
    assert set(by_kind) == {DRIFT_KIND, VERIFY_REJECTED_KIND}
    assert by_kind[DRIFT_KIND]["since"] == "2026-08-04"


def test_two_kinds_for_one_page_are_separate_queue_items(
    tmp_path, write_vault_manifest, monkeypatch
):
    """One page can be wrong in more than one way at once, and each way is
    its own decision for the owner.

    Dedup keys on ``(kind, page)``; drop the kind and the second problem
    for a page that already has an entry is swallowed on arrival -- no
    exception, no eviction count, no digest line, just a decision the
    owner is never asked to make."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: None,
    )
    page = "compiled/topics/aurora.md"
    with decisions_queue.vault_write_lock(vault) as lock:
        append_decision_queue_entries(
            vault,
            [
                {
                    "kind": "fact-check-rejected",
                    "page": page,
                    "summary": "проверка фактов не прошла",
                    "since": "2026-07-10",
                }
            ],
            existing_lock=lock,
        )
        append_decision_queue_entries(
            vault,
            [
                {
                    "kind": BLOCKED_ACTION_KIND,
                    "page": page,
                    "summary": "действие заблокировано уровнем доверия",
                    "since": "2026-07-11",
                }
            ],
            existing_lock=lock,
        )

    queued = json.loads(
        (vault / decisions_queue.DECISIONS_QUEUE_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    assert {item["kind"] for item in queued} == {
        "fact-check-rejected",
        BLOCKED_ACTION_KIND,
    }
    assert {item["page"] for item in queued} == {page}


def test_append_decision_queue_entries_reuses_held_lock_no_deadlock(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Exercises the exact shape both real producers use: the caller opens
    ``vault_write_lock`` itself, then calls ``append_decision_queue_entries``
    from inside it."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    doc_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: doc_calls.append(kwargs),
    )
    outcome: dict[str, object] = {}

    def worker() -> None:
        with decisions_queue.vault_write_lock(vault) as lock:
            append_decision_queue_entries(
                vault,
                [
                    {
                        "kind": "fact-check-rejected",
                        "page": "compiled/topics/aurora.md",
                        "summary": "s",
                        "since": "2026-07-01",
                    }
                ],
                existing_lock=lock,
            )
            outcome["lock"] = lock
        outcome["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), (
        "append_decision_queue_entries self-deadlocked while its caller "
        "already held the vault write lock"
    )
    assert outcome.get("done") is True
    assert len(doc_calls) == 1
    assert doc_calls[0]["existing_lock"] is outcome["lock"]


def test_append_decision_queue_entries_without_held_lock_skips_regeneration(
    tmp_path, caplog
):
    """No current producer calls ``append_decision_queue_entries`` without
    passing its already-held vault lock as ``existing_lock`` (see module
    docstring), but a future one getting that wrong must not crash and,
    above all, must not try to acquire a fresh lock -- it must simply leave
    the mirror stale."""
    vault = tmp_path / "vault"
    vault.mkdir()

    with caplog.at_level("WARNING", logger="d_brain.services.decisions_queue"):
        append_decision_queue_entries(
            vault,
            [
                {
                    "kind": "fact-check-rejected",
                    "page": "compiled/topics/aurora.md",
                    "summary": "s",
                    "since": "2026-07-01",
                }
            ],
        )

    assert not queue_document_path(vault).exists()
    assert "decisions queue mirror not regenerated" in caplog.text


def test_a_failing_mirror_write_does_not_swallow_the_eviction_count(
    tmp_path, write_vault_manifest, monkeypatch, caplog
):
    """The JSON queue is already on disk, evictions and all, before the
    Markdown mirror is regenerated. So a failure there must not take the
    return value with it: that count is the only way ТЗ 7.2's "очередь
    вытеснила" line reaches the owner's digest, and an eviction that already
    happened would otherwise be reported as zero -- or not at all."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / ".session").mkdir(parents=True, exist_ok=True)

    def _boom(items):
        raise RuntimeError("mirror render failed")

    # Broken deep inside the real ``write_queue_document``, not by replacing
    # it: that function swallows its own failures by contract, so stubbing it
    # out would have tested a scenario production cannot reach and proved
    # nothing about the contract this test exists to pin down.
    monkeypatch.setattr(decisions_queue, "render_queue_document_note", _boom)

    entries = [
        {
            "kind": FACT_CHECK_REJECTED_KIND,
            "page": f"compiled/topics/p{index:02d}.md",
            "summary": f"страница {index}",
            "since": "2026-07-01",
        }
        for index in range(QUEUE_CAP + 2)
    ]

    with caplog.at_level("WARNING", logger="d_brain.services.decisions_queue"):
        with decisions_queue.vault_write_lock(vault) as lock:
            evicted = append_decision_queue_entries(
                vault, entries, existing_lock=lock
            )

    assert evicted == 2
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(queue) == QUEUE_CAP
    assert "Failed to write decisions queue document" in caplog.text


def test_run_monthly_fact_check_regenerates_queue_document_without_deadlock(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Месячная проверка фактов (``compiled_fact_check.py``) reaches
    ``append_decision_queue_entries`` from inside ``run_monthly_fact_check``'s
    own already-open ``vault_write_lock`` -- running it for real proves that
    call path does not self-deadlock."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        last_verified="2026-06-01",
        confidence="high",
        sources_rows=[
            ("2026-07-01", "daily/2099-01-01.md", "не подтвердилось A"),
            ("2026-07-02", "daily/2099-02-02.md", "не подтвердилось B"),
        ],
    )
    monkeypatch.setattr(
        compiled_fact_check, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )
    doc_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: doc_calls.append(kwargs),
    )
    result: dict[str, object] = {}

    def worker() -> None:
        result["value"] = run_monthly_fact_check(vault, today=DAY)
        result["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), (
        "run_monthly_fact_check self-deadlocked while regenerating the "
        "decisions queue mirror"
    )
    assert result.get("done") is True
    assert result["value"]["pages_flagged"] == 1
    assert len(doc_calls) == 1


def test_compiled_briefings_duplicate_candidate_producer_no_deadlock(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Ночной проход обогащения (``compiled_briefings.py``) reaches
    ``append_decision_queue_entries`` the same way: from inside
    ``_upsert_briefing``'s own already-open ``vault_write_lock``, passed
    through explicitly (see ``_queue_duplicate_candidate``'s docstring).
    Calling the real method directly here proves that call path does not
    self-deadlock either."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    service = CompiledBriefingService(vault)
    doc_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: doc_calls.append(kwargs),
    )
    outcome: dict[str, object] = {}

    def worker() -> None:
        with decisions_queue.vault_write_lock(vault) as lock:
            service._queue_duplicate_candidate(
                page_rel_path="compiled/projects/new.md",
                candidate_rel_path="compiled/projects/old.md",
                confidence=0.9,
                existing_lock=lock,
            )
            outcome["lock"] = lock
        outcome["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), (
        "the nightly-enrichment duplicate-candidate producer self-deadlocked "
        "while its caller already held the vault write lock"
    )
    assert outcome.get("done") is True
    assert len(doc_calls) == 1
    assert doc_calls[0]["existing_lock"] is outcome["lock"]


# --- apply_response: fact-check-rejected -------------------------------------


def test_apply_response_fact_check_confirm_patches_and_removes_from_queue(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    recorded: dict[str, object] = {}

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        recorded["updates"] = updates

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "confirm", today=DAY)

    assert isinstance(outcome, ResponseOutcome)
    assert outcome.ok is True
    assert recorded["updates"] == {"last_verified": "2026-08-05"}
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_reject_archives_and_removes_from_queue(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    archived: list[str] = []

    def _fake_archive(self, candidate):
        archived.append(candidate.rel_path)
        return "compiled/archive/topics/aurora.md"

    monkeypatch.setattr(CompiledBriefingService, "_archive_candidate", _fake_archive)
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "reject")

    assert outcome.ok is True
    assert archived == ["compiled/topics/aurora.md"]
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_reject_missing_page_does_not_claim_archived(
    tmp_path, write_vault_manifest
):
    """Code review defect 6: when the page is already gone, ``_archive_candidate``
    is never even called (no candidate found), so the outcome message must
    not claim "отправлена в архив" -- it never happened."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/ghost.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "reject")

    assert outcome.ok is True
    assert "отправлена в архив" not in outcome.message
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_confirm_missing_parent_directory_clears_queue(
    tmp_path, write_vault_manifest
):
    """Code review defect 2: ``patch_validated_vault_frontmatter`` raises
    ``UnsafeVaultPathError`` (a ``ValueError`` subclass), not
    ``FileNotFoundError``, when the page's parent directory does not exist
    at all -- the previous ``except FileNotFoundError`` never caught it, so
    the stale queue entry was never cleared and the exception propagated.
    Deliberately does not monkeypatch ``patch_validated_vault_frontmatter``:
    it fails safely (no parent directory to write into) before ever
    reaching the CAS commit step this sandbox cannot exercise for real.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/ghost-domain/missing.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "confirm", today=DAY)

    assert outcome.ok is True
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_confirm_missing_file_clears_queue(
    tmp_path, write_vault_manifest
):
    """Code review defect 2, second scenario: parent directory exists but
    the page file itself does not -- same ``UnsafeVaultPathError`` outcome,
    exercised without monkeypatching the frontmatter patch function."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled" / "topics").mkdir(parents=True)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/ghost.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "confirm", today=DAY)

    assert outcome.ok is True
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_confirm_missing_page_does_not_claim_updated(
    tmp_path, write_vault_manifest
):
    """Mirrors test_apply_response_fact_check_reject_missing_page_does_not_
    claim_archived: when the page is already gone,
    patch_validated_vault_frontmatter raises UnsafeVaultPathError and
    nothing is actually patched, so the outcome
    message must not claim "дата проверки обновлена" -- it never happened."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled" / "topics").mkdir(parents=True)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/ghost.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "confirm", today=DAY)

    assert outcome.ok is True
    assert "дата проверки обновлена" not in outcome.message
    assert "отсутствовала" in outcome.message
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_fact_check_defer_is_noop(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "defer")

    assert outcome.ok is True
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert len(queue) == 1  # untouched


# --- apply_response: conflict -------------------------------------------------


def _install_fake_page_writer(monkeypatch) -> None:
    def _fake_write(vault_path, path, content, *, manifest=None, existing_lock=None,
                     preserve_existing_body=False, expected_full_sha256=None,
                     require_absent=False):
        Path(path).write_bytes(content)

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)


def _spy_on_vault_write_lock(monkeypatch) -> list[object]:
    """Patch ``decisions_queue.vault_write_lock`` to record every lock
    instance it yields, so a test can assert a later write received that
    exact object -- same technique as
    ``test_apply_response_conflict_choice_passes_existing_lock_to_page_write``
    (задача N: reused for every other mutating handler below)."""
    captured_locks: list[object] = []
    real_vault_write_lock = decisions_queue.vault_write_lock

    @contextmanager
    def _spying(vault_path):
        with real_vault_write_lock(vault_path) as lock:
            captured_locks.append(lock)
            yield lock

    monkeypatch.setattr(decisions_queue, "vault_write_lock", _spying)
    return captured_locks


def test_apply_response_conflict_choice_removes_row_and_decrements_count(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    _install_fake_page_writer(monkeypatch)
    item = list_queue_items(vault)[0]
    assert item.kind == CONFLICT_KIND

    outcome = apply_response(vault, item, "keep_new")

    assert outcome.ok is True
    assert "Контракт на два года" in outcome.message
    new_text = (vault / "compiled/topics/aurora.md").read_text(encoding="utf-8")
    assert CompiledBriefingService._open_conflicts_rows(new_text) == []
    fields = CompiledBriefingService._frontmatter_fields(new_text)
    assert fields["conflicts_open"] == "0"


def test_apply_response_conflict_choice_refuses_undecodable_page_bytes(
    tmp_path, write_vault_manifest, monkeypatch
):
    """A page whose bytes are not valid UTF-8 reaches the queue anyway --
    ``list_queue_items`` reads pages with ``errors="replace"`` -- so the
    owner can tap an answer for it. This handler rewrites the whole page,
    so it can neither decode leniently (that would write U+FFFD over the
    real bytes) nor decode strictly and raise: the bot handler catches the
    resulting ``UnicodeDecodeError`` as a ``ValueError`` and shows "action
    unavailable", leaving the page silently unresolvable. Fail closed with
    a message that names the actual problem, and touch nothing."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "Первое обновление")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    page_path = vault / "compiled/topics/aurora.md"
    original = page_path.read_bytes().replace(
        "Первое обновление".encode(), b"\xff\xfe"
    )
    page_path.write_bytes(original)
    _install_fake_page_writer(monkeypatch)
    item = list_queue_items(vault)[0]
    assert item.kind == CONFLICT_KIND

    outcome = apply_response(vault, item, "keep_new")

    assert outcome.ok is False
    assert "UTF-8" in outcome.message
    assert page_path.read_bytes() == original


def test_apply_response_conflict_choice_sources_table_stays_byte_identical(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Invariant required by задача L: resolving a conflict never shrinks or
    otherwise touches the "Sources That Shaped This Page" table -- only the
    "Open Conflicts" table and the ``conflicts_open`` counter change."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[
            ("2026-07-01", "daily/a.md", "Первое обновление"),
            ("2026-07-10", "daily/c.md", "Второе обновление"),
        ],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    page_path = vault / "compiled/topics/aurora.md"
    before_sources = _sources_table_section(page_path.read_text(encoding="utf-8"))
    _install_fake_page_writer(monkeypatch)
    item = list_queue_items(vault)[0]

    apply_response(vault, item, "keep_existing")

    after_sources = _sources_table_section(page_path.read_text(encoding="utf-8"))
    assert after_sources == before_sources


def test_apply_response_conflict_choice_idempotent_when_row_already_gone(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    item = list_queue_items(vault)[0]
    # The row is resolved by another process/response before this one runs.
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[],
        conflicts_open=0,
    )
    # No write should be attempted -- leaving write_validated_vault_markdown
    # unpatched would raise UnsafeVaultPathError in this sandbox if it were
    # (wrongly) called; a clean pass through this test proves it was not.

    outcome = apply_response(vault, item, "keep_existing")

    assert outcome.ok is True
    assert "уже разрешён" in outcome.message


def test_apply_response_conflict_choice_passes_existing_lock_to_page_write(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Code review defect 1 (critical self-deadlock): the outer
    ``vault_write_lock`` this function holds must be forwarded to
    ``write_validated_vault_markdown`` as ``existing_lock`` -- otherwise
    that call takes its own fresh lock on the same ``vault-write.lock``
    file and blocks forever waiting on the lock this very function is
    still holding (flock is not re-entrant across two open descriptors to
    the same file, even within one process). Reproduced live against this
    module before the fix, with no monkeypatching at all: the real,
    uninstrumented ``apply_response(vault, item, "keep_existing")`` call
    hung and had to be killed by ``timeout``; after passing
    ``existing_lock=lock`` it instead fails fast on the CAS commit step
    this sandbox cannot exercise for real
    (``UnsafeVaultPathError: vault Markdown parent does not exist`` --
    same environment limitation this file's own module docstring
    documents), never blocking.

    This test does not reproduce the actual deadlock (that would hang the
    test suite) -- it spies on the *real* ``vault_write_lock`` to capture
    the lock instance it yields, monkeypatches only the low-level
    ``write_validated_vault_markdown`` (whose CAS commit cannot run for
    real here), and asserts every write call received that exact lock
    object rather than none/a different one.

    Two writes happen inside this one lock now (задача N): the conflict
    page itself, then the human-readable queue document regenerated at the
    end of the handler (``write_queue_document``) -- both must forward the
    same lock, never open a second one.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    item = list_queue_items(vault)[0]

    captured_locks: list[object] = []
    real_vault_write_lock = decisions_queue.vault_write_lock

    @contextmanager
    def _spying_vault_write_lock(vault_path):
        with real_vault_write_lock(vault_path) as lock:
            captured_locks.append(lock)
            yield lock

    monkeypatch.setattr(decisions_queue, "vault_write_lock", _spying_vault_write_lock)

    write_calls: list[dict[str, object]] = []

    def _fake_write(vault_path, path, content, **kwargs):
        write_calls.append(kwargs)
        Path(path).write_bytes(content)

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)

    apply_response(vault, item, "keep_new")

    assert len(captured_locks) == 1
    assert len(write_calls) == 2
    assert all(call.get("existing_lock") is captured_locks[0] for call in write_calls)


def test_apply_response_conflict_choice_missing_file_returns_friendly_outcome(
    tmp_path, write_vault_manifest
):
    """Code review defect 3: ``read_vault_file_bytes`` raises a bare
    ``FileNotFoundError`` (unlike the frontmatter-patch helpers, which wrap
    it as ``UnsafeVaultPathError``) when the conflict item's page vanished
    between listing and responding. The previous code read it unguarded,
    so this reached the caller as an uncaught exception. Real call, no
    monkeypatching of ``read_vault_file_bytes``: the parent directory
    exists, only the file itself is gone.
    """
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    item = list_queue_items(vault)[0]
    (vault / "compiled/topics/aurora.md").unlink()

    outcome = apply_response(vault, item, "keep_existing")

    assert outcome.ok is True
    assert "пропала" in outcome.message


def test_apply_response_conflict_choice_missing_parent_dir_returns_friendly_outcome(
    tmp_path, write_vault_manifest
):
    """Code review defect 3, second scenario: the whole domain directory is
    gone, not just the file -- same bare ``FileNotFoundError`` from
    ``read_vault_file_bytes``, same graceful outcome expected."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15",
        "Контракт на год",
        "daily/a.md",
        "Контракт на два года",
        "daily/b.md",
    )
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row],
        conflicts_open=1,
    )
    item = list_queue_items(vault)[0]
    (vault / "compiled/topics/aurora.md").unlink()
    (vault / "compiled/topics").rmdir()

    outcome = apply_response(vault, item, "keep_existing")

    assert outcome.ok is True
    assert "пропала" in outcome.message


# --- queue_item_fingerprint (задача L code review, defect 4) -----------------


def test_queue_item_fingerprint_is_stable_and_8_hex_chars():
    item = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    fingerprint = queue_item_fingerprint(item)

    assert fingerprint == queue_item_fingerprint(item)
    assert len(fingerprint) == 8
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_queue_item_fingerprint_differs_for_different_identity():
    base = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )
    different_page = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/other.md",
        summary="s", since="2026-07-01",
    )
    different_since = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-02",
    )
    different_kind = QueueItem(
        kind=FACT_CHECK_REJECTED_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    fingerprints = {
        queue_item_fingerprint(base),
        queue_item_fingerprint(different_page),
        queue_item_fingerprint(different_since),
        queue_item_fingerprint(different_kind),
    }

    assert len(fingerprints) == 4  # all distinct -- summary is not part of identity


def test_queue_item_fingerprint_distinguishes_conflict_row():
    """Code review: candidate_page is always empty for kind == "conflict", so
    two different open conflicts on the same page dated the same day (e.g.
    two sources landing the same day) would fingerprint identically without
    conflict_row in the hash, letting a stale button silently apply the
    owner's choice to the wrong conflict."""
    conflict_a = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
        conflict_row=("2026-07-01", "claim-a", "daily/a.md", "claim-b", "daily/b.md"),
    )
    conflict_b = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
        conflict_row=("2026-07-01", "claim-c", "daily/c.md", "claim-d", "daily/d.md"),
    )

    assert queue_item_fingerprint(conflict_a) != queue_item_fingerprint(conflict_b)


def test_queue_item_fingerprint_stable_for_same_conflict_row():
    conflict = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
        conflict_row=("2026-07-01", "claim-a", "daily/a.md", "claim-b", "daily/b.md"),
    )
    same_conflict_different_summary = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="a different summary string",
        since="2026-07-01",
        conflict_row=("2026-07-01", "claim-a", "daily/a.md", "claim-b", "daily/b.md"),
    )

    assert queue_item_fingerprint(conflict) == queue_item_fingerprint(
        same_conflict_different_summary
    )


def test_queue_item_fingerprint_distinguishes_separator_injection_in_row():
    """Code review: a plain ``\\x1f``-joined fingerprint can collide when the
    separator character itself appears inside a field's value -- the row
    fields come from the "Open Conflicts" table, i.e. ultimately model
    output, so a stray ``\\x1f`` inside a claim/source string is not
    something this module can rule out upstream. Shifting one ``\\x1f`` from
    the end of ``existing_claim`` to the start of ``existing_source``
    produces the same naively-joined string, and previously the same
    fingerprint -- a stale button could then silently apply the owner's
    choice to the wrong conflict row."""
    row_a = ("2026-07-01", "existing_claim\x1fX", "es", "nc", "ns")
    row_b = ("2026-07-01", "existing_claim", "X\x1fes", "nc", "ns")
    item_a = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
        conflict_row=row_a,
    )
    item_b = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
        conflict_row=row_b,
    )

    assert queue_item_fingerprint(item_a) != queue_item_fingerprint(item_b)


def test_queue_item_fingerprint_distinguishes_a_shifted_field_boundary():
    """The length-prefixed ("netstring") encoding is what makes the field
    boundaries unambiguous: with the fields simply concatenated, moving one
    character from the end of ``page`` to the start of ``since`` leaves the
    concatenation byte-identical, so two genuinely different queue items
    would share a fingerprint and a stale button could apply the owner's
    answer to the wrong one."""
    item_a = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md2",
        summary="s", since="026-07-01",
    )
    item_b = QueueItem(
        kind=CONFLICT_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    assert queue_item_fingerprint(item_a) != queue_item_fingerprint(item_b)


# --- apply_response: unknown kind / defer / validation -----------------------


def test_apply_response_unknown_kind_reject_removes_from_queue(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "drift",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "reject")

    assert outcome.ok is True
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_defer_never_mutates_vault_and_journals(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = QueueItem(
        kind=FACT_CHECK_REJECTED_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    apply_response(vault, item, "defer")

    journal_path = vault / ".session" / "decisions-queue-responses.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "defer"
    assert entry["page"] == "compiled/topics/aurora.md"


def test_apply_response_journal_is_separate_from_pass_journals(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = QueueItem(
        kind=FACT_CHECK_REJECTED_KIND, page="compiled/topics/aurora.md",
        summary="s", since="2026-07-01",
    )

    apply_response(vault, item, "defer")

    session_dir = vault / ".session"
    assert (session_dir / "decisions-queue-responses.jsonl").exists()
    assert not (session_dir / "compile-enrich.json").exists()
    assert not (session_dir / "compile-fact-check.json").exists()


def test_apply_response_rejects_action_not_offered_for_kind(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = QueueItem(
        kind="drift", page="compiled/topics/aurora.md", summary="s", since=""
    )

    with pytest.raises(ValueError):
        apply_response(vault, item, "confirm")


# --- shared queue-line rendering (задача N) -----------------------------------


def test_queue_kind_label_known_and_unknown_kinds():
    assert queue_kind_label(FACT_CHECK_REJECTED_KIND) == "проверка фактов"
    assert queue_kind_label(CONFLICT_KIND) == "конфликт"
    assert queue_kind_label(DUPLICATE_CANDIDATE_KIND) == "возможный дубликат"
    assert queue_kind_label(DRIFT_KIND) == "дрейф"
    assert queue_kind_label(BLOCKED_ACTION_KIND) == "действие заблокировано"
    assert queue_kind_label(PAGE_ENCODING_BROKEN_KIND) == "кодировка файла страницы"
    assert queue_kind_label(HUMAN_ZONE_AMBIGUOUS_KIND) == "маркеры личной зоны"
    # A genuinely unrecognized kind still gets an honest, readable "unknown"
    # label, not a blank one.
    assert queue_kind_label("made-up-kind") == "неизвестный тип: made-up-kind"


def test_format_queue_item_line_matches_kind_page_summary():
    item = QueueItem(
        kind=CONFLICT_KIND,
        page="compiled/decisions/vendor.md",
        summary="старое против нового",
        since="2026-07-01",
    )

    line = format_queue_item_line(item)

    assert line == "- [конфликт] compiled/decisions/vendor.md — старое против нового"


def test_render_queue_document_empty_queue_gets_placeholder_not_empty_file():
    """Задача N explicit requirement: an empty queue must render an explicit
    placeholder line, not an empty file."""
    body = render_queue_document([])

    assert body.strip()
    assert "пуста" in body.lower()


def test_render_queue_document_uses_format_queue_item_line_for_each_item():
    """Invariant: the file and the bot's queue screen must build each line
    with the exact same function, so they can never say something different
    about the same item."""
    items = [
        QueueItem(
            kind=CONFLICT_KIND, page="compiled/topics/a.md", summary="s1",
            since="2026-07-01",
        ),
        QueueItem(
            kind=FACT_CHECK_REJECTED_KIND, page="compiled/topics/b.md", summary="s2",
            since="2026-07-02",
        ),
    ]

    body = render_queue_document(items)

    for item in items:
        assert format_queue_item_line(item) in body


def test_queue_document_path_lives_under_summaries():
    path = queue_document_path(Path("/vault"))

    assert path == Path("/vault/summaries/compile/decisions-queue.md")


# --- write_queue_document: regeneration hooks (задача N) ---------------------


def test_write_queue_document_passes_manifest_and_existing_lock_through(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    manifest = load_manifest_for_vault(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    write_calls: list[dict[str, object]] = []

    def _fake_write(vault_path, path, content, **kwargs):
        write_calls.append({"path": path, "content": content, **kwargs})

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)
    sentinel_lock = object()

    write_queue_document(vault, manifest=manifest, existing_lock=sentinel_lock)

    assert len(write_calls) == 1
    assert write_calls[0]["path"] == queue_document_path(vault)
    assert write_calls[0]["existing_lock"] is sentinel_lock
    assert write_calls[0]["manifest"] is manifest
    assert b"fact-check-rejected" not in write_calls[0]["content"]  # rendered, not raw
    assert b"compiled/topics/aurora.md" in write_calls[0]["content"]


def test_write_queue_document_best_effort_swallows_write_failures(
    tmp_path, write_vault_manifest, caplog
):
    """Best-effort by design (задача N): by the time any caller reaches
    this, the real decision already committed, so a failure to refresh this
    mirror file must never raise -- only log. Deliberately does not
    monkeypatch the writer: the real ``write_validated_vault_markdown``
    fails in this sandbox (see module docstring), which is exactly the
    failure path being exercised."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    manifest = load_manifest_for_vault(vault)

    with caplog.at_level("WARNING", logger="d_brain.services.decisions_queue"):
        write_queue_document(vault, manifest=manifest, existing_lock=object())

    assert "Failed to write decisions queue document" in caplog.text


def test_apply_response_fact_check_confirm_regenerates_queue_document_same_lock(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter",
        lambda *a, **k: None,
    )
    captured_locks = _spy_on_vault_write_lock(monkeypatch)
    doc_calls: list[dict[str, object]] = []

    def _fake_write(vault_path, path, content, **kwargs):
        doc_calls.append(kwargs)

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)
    item = list_queue_items(vault)[0]

    apply_response(vault, item, "confirm", today=DAY)

    assert len(captured_locks) == 1
    assert len(doc_calls) == 1
    assert doc_calls[0]["existing_lock"] is captured_locks[0]


def test_apply_response_fact_check_reject_regenerates_queue_document_same_lock(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    _write_queue(
        vault,
        [
            {
                "kind": "fact-check-rejected",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    monkeypatch.setattr(
        CompiledBriefingService, "_archive_candidate",
        lambda self, candidate: "compiled/archive/topics/aurora.md",
    )
    captured_locks = _spy_on_vault_write_lock(monkeypatch)
    doc_calls: list[dict[str, object]] = []

    def _fake_write(vault_path, path, content, **kwargs):
        doc_calls.append(kwargs)

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)
    item = list_queue_items(vault)[0]

    apply_response(vault, item, "reject")

    assert len(captured_locks) == 1
    assert len(doc_calls) == 1
    assert doc_calls[0]["existing_lock"] is captured_locks[0]


def test_apply_response_unknown_reject_now_takes_lock_and_regenerates_document(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Задача N code review: ``_apply_unknown_reject`` previously took no
    vault write lock at all, unlike every sibling handler -- this both
    proves the fix (the spy only sees locks acquired through
    ``vault_write_lock``, so no captured lock would mean it still bypasses
    the lock) and that it now regenerates the queue document like the rest."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "drift",
                "page": "compiled/topics/aurora.md",
                "summary": "s",
                "since": "2026-07-01",
            }
        ],
    )
    captured_locks = _spy_on_vault_write_lock(monkeypatch)
    doc_calls: list[dict[str, object]] = []

    def _fake_write(vault_path, path, content, **kwargs):
        doc_calls.append(kwargs)

    monkeypatch.setattr(decisions_queue, "write_validated_vault_markdown", _fake_write)
    item = list_queue_items(vault)[0]

    apply_response(vault, item, "reject")

    assert len(captured_locks) == 1
    assert len(doc_calls) == 1
    assert doc_calls[0]["existing_lock"] is captured_locks[0]


def test_apply_response_conflict_choice_idempotent_noop_does_not_regenerate_document(
    tmp_path, write_vault_manifest, monkeypatch
):
    """The two early-return no-op paths (page vanished / conflict already
    resolved) must not regenerate the queue document -- nothing changed, so
    there is nothing new to mirror. Deliberately does not monkeypatch
    ``write_validated_vault_markdown``: a call would raise
    ``UnsafeVaultPathError`` in this sandbox, which a clean pass proves did
    not happen."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    row = (
        "2026-07-15", "Контракт на год", "daily/a.md",
        "Контракт на два года", "daily/b.md",
    )
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[row], conflicts_open=1,
    )
    item = list_queue_items(vault)[0]
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
        conflicts_rows=[], conflicts_open=0,
    )

    outcome = apply_response(vault, item, "keep_existing")

    assert outcome.ok is True
    assert "уже разрешён" in outcome.message


# --- duplicate-candidate kind (задача N) --------------------------------------


def test_list_queue_items_parses_duplicate_candidate_second_page(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_queue(
        vault,
        [
            {
                "kind": "duplicate-candidate",
                "page": "compiled/topics/new-page.md",
                "candidate_page": "compiled/topics/old-page.md",
                "summary": "похожие страницы",
                "since": "2026-07-01",
            }
        ],
    )

    item = list_queue_items(vault)[0]

    assert item.kind == DUPLICATE_CANDIDATE_KIND
    assert item.page == "compiled/topics/new-page.md"
    assert item.candidate_page == "compiled/topics/old-page.md"


def test_action_button_specs_duplicate_candidate_offers_link_distinct_defer():
    item = QueueItem(
        kind=DUPLICATE_CANDIDATE_KIND,
        page="compiled/topics/new-page.md",
        candidate_page="compiled/topics/old-page.md",
        summary="s",
        since="2026-07-01",
    )

    ids = {action_id for action_id, _label in action_button_specs(item)}

    assert ids == {"link", "distinct", "defer"}


def test_queue_item_fingerprint_distinguishes_candidate_page():
    """Задача N: without candidate_page in the hash, two different
    duplicate-candidate proposals about the same page reported the same day
    would fingerprint identically, letting a stale button silently resolve
    to the wrong candidate."""
    proposal_a = QueueItem(
        kind=DUPLICATE_CANDIDATE_KIND, page="compiled/topics/new.md",
        candidate_page="compiled/topics/old-a.md", summary="s", since="2026-07-01",
    )
    proposal_b = QueueItem(
        kind=DUPLICATE_CANDIDATE_KIND, page="compiled/topics/new.md",
        candidate_page="compiled/topics/old-b.md", summary="s", since="2026-07-01",
    )

    assert queue_item_fingerprint(proposal_a) != queue_item_fingerprint(proposal_b)


def _write_duplicate_queue(vault: Path) -> QueueItem:
    _write_queue(
        vault,
        [
            {
                "kind": "duplicate-candidate",
                "page": "compiled/topics/new-page.md",
                "candidate_page": "compiled/topics/old-page.md",
                "summary": "похожие страницы",
                "since": "2026-07-01",
            }
        ],
    )
    return list_queue_items(vault)[0]


def test_apply_response_duplicate_link_patches_both_pages_mutually(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Testing invariant: linking edits both pages."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = _write_duplicate_queue(vault)
    patched: list[tuple[str, dict[str, object]]] = []

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        patched.append((Path(path).relative_to(vault_path).as_posix(), updates))

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )
    monkeypatch.setattr(
        decisions_queue, "write_validated_vault_markdown", lambda *a, **k: None
    )

    outcome = apply_response(vault, item, "link")

    assert outcome.ok is True
    assert (
        "compiled/topics/new-page.md",
        {"duplicate_of": "compiled/topics/old-page.md"},
    ) in patched
    assert (
        "compiled/topics/old-page.md",
        {"duplicate_of": "compiled/topics/new-page.md"},
    ) in patched
    assert len(patched) == 2
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_duplicate_link_soft_fails_independently_per_vanished_page(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Each page's marking uses its own independent soft-fail handling: one
    page vanishing must not block marking the other."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = _write_duplicate_queue(vault)

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        if "old-page" in str(path):
            raise decisions_queue.UnsafeVaultPathError("gone")

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )
    monkeypatch.setattr(
        decisions_queue, "write_validated_vault_markdown", lambda *a, **k: None
    )

    outcome = apply_response(vault, item, "link")

    assert outcome.ok is True
    assert "new-page.md" in outcome.message
    assert "пропала" in outcome.message


def test_apply_response_duplicate_distinct_touches_neither_page(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Testing invariant: marking-different edits neither page."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = _write_duplicate_queue(vault)
    patched: list[str] = []

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        patched.append(str(path))

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )
    monkeypatch.setattr(
        decisions_queue, "write_validated_vault_markdown", lambda *a, **k: None
    )

    outcome = apply_response(vault, item, "distinct")

    assert outcome.ok is True
    assert patched == []
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []


def test_apply_response_duplicate_reject_action_not_offered(
    tmp_path, write_vault_manifest
):
    """"reject" is a fact-check/unknown-kind action, never offered for
    duplicate-candidate -- must raise rather than silently doing nothing."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = _write_duplicate_queue(vault)

    with pytest.raises(ValueError):
        apply_response(vault, item, "reject")


# --- verify-rejected (code review defect 2: Verify rejections must reach the
# owner's decisions queue, ТЗ 5.2/7.2) --------------------------------------


def test_action_button_specs_verify_rejected_offers_retry_reject_defer(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    item = QueueItem(
        kind=VERIFY_REJECTED_KIND,
        page="compiled/topics/aurora.md",
        summary="Verify отклонил больше половины утверждений",
        since="2026-08-01",
    )

    ids = {action_id for action_id, _label in action_button_specs(item)}

    assert ids == {"retry", "reject", "defer"}


def test_apply_response_verify_rejected_retry_clears_counter_and_queue_entry(
    tmp_path, write_vault_manifest, monkeypatch
):
    """"Повторить проверку" must reset the page's Verify-rejection retry
    counter (``CompiledBriefingService._clear_verify_rejection``) so the
    next enrichment pass attempts Verify again instead of immediately
    treating the page as still exhausted -- and must remove the queue
    entry so the owner is not asked twice."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    service = CompiledBriefingService(vault)
    rel_path = "compiled/topics/aurora.md"
    for _ in range(3):
        service._record_verify_rejection(rel_path, "текст страницы")
    state_before = json.loads(
        service.source_state_path.read_text(encoding="utf-8")
    )
    assert "verify_rejected" in state_before["entries"][rel_path]

    monkeypatch.setattr(
        decisions_queue, "write_validated_vault_markdown", lambda *a, **k: None
    )
    spawned: list[bool] = []
    monkeypatch.setattr(
        CompiledBriefingService,
        "spawn_background_drain",
        lambda self: spawned.append(True) or True,
    )
    _write_queue(
        vault,
        [
            {
                "kind": VERIFY_REJECTED_KIND,
                "page": rel_path,
                "summary": "Verify отклонил больше половины утверждений",
                "since": "2026-08-01",
                "source_path": "daily/2026-08-05.md",
                "source_excerpt": "исходный фрагмент",
                "max_updates": "2",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "retry")

    assert outcome.ok is True
    assert rel_path in outcome.message
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []
    state_after = json.loads(
        service.source_state_path.read_text(encoding="utf-8")
    )
    assert "verify_rejected" not in state_after["entries"][rel_path]
    compile_queue = json.loads(
        (vault / ".compiled" / "queue.json").read_text(encoding="utf-8")
    )
    assert len(compile_queue) == 1
    assert compile_queue[0]["source_path"] == "daily/2026-08-05.md"
    assert compile_queue[0]["source_excerpt"] == "исходный фрагмент"
    assert compile_queue[0]["max_updates"] == 2
    assert spawned == [True]
    journal_path = vault / ".session" / "decisions-queue-responses.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "retry"
    assert entry["page"] == rel_path


def test_apply_response_verify_rejected_reject_leaves_counter_untouched(
    tmp_path, write_vault_manifest, monkeypatch
):
    """"Прекратить попытки" only removes the queue entry (generic
    ``_apply_unknown_reject`` path) -- it must not reach into
    ``CompiledBriefingService``'s retry counter, which "retry" owns."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)
    service = CompiledBriefingService(vault)
    rel_path = "compiled/topics/aurora.md"
    service._record_verify_rejection(rel_path, "текст страницы")

    monkeypatch.setattr(
        decisions_queue, "write_validated_vault_markdown", lambda *a, **k: None
    )
    _write_queue(
        vault,
        [
            {
                "kind": VERIFY_REJECTED_KIND,
                "page": rel_path,
                "summary": "Verify отклонил больше половины утверждений",
                "since": "2026-08-01",
            }
        ],
    )
    item = list_queue_items(vault)[0]

    outcome = apply_response(vault, item, "reject")

    assert outcome.ok is True
    queue = json.loads(
        (vault / ".session" / "decisions-queue.json").read_text(encoding="utf-8")
    )
    assert queue == []
    state_after = json.loads(
        service.source_state_path.read_text(encoding="utf-8")
    )
    assert "verify_rejected" in state_after["entries"][rel_path]


# --- mark_page_human_reviewed (задача N, weekly review) -----------------------


def test_mark_page_human_reviewed_patches_frontmatter_field(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    recorded: dict[str, object] = {}

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        recorded["updates"] = updates

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )

    outcome = mark_page_human_reviewed(
        vault, "compiled/topics/aurora.md", today=DAY
    )

    assert outcome.ok is True
    assert recorded["updates"] == {"human_reviewed": "2026-08-05"}


def test_mark_page_human_reviewed_passes_existing_lock_through(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    captured_locks = _spy_on_vault_write_lock(monkeypatch)
    patch_calls: list[dict[str, object]] = []

    def _fake_patch(vault_path, path, updates, *, manifest=None, existing_lock=None):
        patch_calls.append({"existing_lock": existing_lock})

    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", _fake_patch
    )

    mark_page_human_reviewed(vault, "compiled/topics/aurora.md", today=DAY)

    assert len(captured_locks) == 1
    assert patch_calls[0]["existing_lock"] is captured_locks[0]


def test_mark_page_human_reviewed_missing_page_soft_fails(
    tmp_path, write_vault_manifest
):
    """Deliberately does not monkeypatch the patch function: a missing
    parent directory fails safely with the real ``UnsafeVaultPathError``
    (same convention as the fact-check confirm handler's own soft-fail
    tests)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)

    outcome = mark_page_human_reviewed(
        vault, "compiled/topics/ghost.md", today=DAY
    )

    assert outcome.ok is True
    assert "пропала" in outcome.message


def test_mark_page_human_reviewed_does_not_touch_decisions_queue(
    tmp_path, write_vault_manifest, monkeypatch
):
    """Not a queue response -- must not call write_queue_document (no
    summaries/compile/decisions-queue.md mirror write attempted)."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault, "compiled/topics/aurora.md",
        sources_rows=[("2026-07-01", "daily/a.md", "x")],
    )
    monkeypatch.setattr(
        decisions_queue, "patch_validated_vault_frontmatter", lambda *a, **k: None
    )
    write_calls: list[object] = []
    monkeypatch.setattr(
        decisions_queue,
        "write_validated_vault_markdown",
        lambda *a, **k: write_calls.append((a, k)),
    )

    mark_page_human_reviewed(vault, "compiled/topics/aurora.md", today=DAY)

    assert write_calls == []
