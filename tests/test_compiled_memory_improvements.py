from pathlib import Path

import pytest
from conftest import _write_vault_manifest

from d_brain.services.compiled_briefings import (
    CompiledBriefingCandidate,
    CompiledBriefingService,
    CompiledBriefingTarget,
)


def _service(tmp_path: Path) -> CompiledBriefingService:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_vault_manifest(vault_path)
    return CompiledBriefingService(vault_path)


def test_partial_update_keeps_unmentioned_sections_and_owner_zone_verbatim(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    existing = (
        "---\ndomain: projects\n---\n\n# Demo\n\n"
        "## Current State\nOld state.\n\n"
        "## Recent Changes\n- Keep this exact change.  \n\n\n"
        "## Sources\n- [[daily/old.md]]\n\n"
        "## Sources That Shaped This Page\n| Date | Source | What changed |\n"
        "| --- | --- | --- |\n| 2026-01-01 | [[daily/old.md]] | Old. |\n\n"
        "## Open Conflicts\n(none)\n\n## Claim History\n(none)\n\n"
        "## Owner Notes\n<!-- human:start -->\nOwner text.\n<!-- human:end -->\n"
    )
    rendered = existing.replace("Old state.", "New state.").replace(
        "[[daily/old.md]]", "[[daily/new.md]]", 1
    )

    merged = service._apply_partial_sections(
        existing_text=existing,
        rendered=rendered,
        payload={"changed_sections": ["Current State"]},
    )

    assert service._section_text(merged, "Current State") == "New state."
    assert "## Recent Changes\n- Keep this exact change.  \n\n\n" in merged
    assert "<!-- human:start -->\nOwner text.\n<!-- human:end -->" in merged
    assert "[[daily/new.md]]" in service._section_text(merged, "Sources")


def test_partial_update_rejects_protected_or_missing_section(tmp_path: Path) -> None:
    service = _service(tmp_path)
    page = (
        "## Current State\nOld.\n\n## Owner Notes\n"
        "<!-- human:start -->\n<!-- human:end -->\n"
    )

    with pytest.raises(ValueError, match="protected or unknown"):
        service._apply_partial_sections(
            existing_text=page,
            rendered=page,
            payload={"changed_sections": ["Sources"]},
        )

    with pytest.raises(ValueError, match="required for an existing"):
        service._apply_partial_sections(
            existing_text=page,
            rendered=page,
            payload={},
        )

    with pytest.raises(ValueError, match="existing rendered"):
        service._apply_partial_sections(
            existing_text=page,
            rendered=page,
            payload={"changed_sections": ["Recent Changes"]},
        )


def test_partial_update_rejects_new_content_heading(tmp_path: Path) -> None:
    service = _service(tmp_path)
    existing = (
        "## Current State\nOld.\n\n## Owner Notes\n"
        "<!-- human:start -->\n<!-- human:end -->\n"
    )
    rendered = (
        "## Incident Debrief\nUnexpected incident.\n\n"
        "## Current State\nNew.\n\n## Owner Notes\n"
        "<!-- human:start -->\n<!-- human:end -->\n"
    )

    with pytest.raises(ValueError, match="must not add content sections"):
        service._apply_partial_sections(
            existing_text=existing,
            rendered=rendered,
            payload={"changed_sections": ["Current State"]},
        )


def test_verify_receives_the_partial_candidate(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    existing = (
        "---\ndomain: projects\n---\n\n# Demo\n\n"
        "## Current State\nOld state.\n\n"
        "## Recent Changes\n- Keep exactly.  \n\n\n"
        "## Open Loops\n(none)\n\n## Key Decisions\n(none)\n\n"
        "## Next Check\nLater.\n\n## Sources\n- [[daily/old.md]]\n\n"
        "## Sources That Shaped This Page\n(none)\n\n"
        "## Open Conflicts\n(none)\n\n## Claim History\n(none)\n\n"
        "## Owner Notes\n<!-- human:start -->\n<!-- human:end -->\n"
    )
    payload = {
        "changed_sections": ["Current State"],
        "current_state": "New state.",
        "recent_changes": [],
        "open_loops": [],
        "key_decisions": [],
        "next_check": "Later.",
        "source_links": [],
        "claims": [{"text": "New state is confirmed.", "kind": "fact"}],
    }
    received: dict[str, str] = {}

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        received["markdown"] = kwargs["candidate_markdown"]
        return kwargs["claims"]

    monkeypatch.setattr(service, "_verify_claims_batch", fake_verify)
    service._extract_and_verify_claims(
        payload=payload,
        target=CompiledBriefingTarget(
            domain="projects",
            title="Demo",
            slug="demo",
            description="Demo",
            reason="test",
        ),
        source_rel_path="daily/2026-10-09.md",
        source_excerpt="New state is confirmed.",
        existing_claims="[]",
        existing_text=existing,
        existing_meta={},
        signal=None,
    )

    assert "## Current State\nNew state." in received["markdown"]
    assert "## Recent Changes\n- Keep exactly.  \n\n\n" in received["markdown"]


def test_late_old_event_cannot_replace_current_claim(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    (service.vault_path / "daily").mkdir()
    (service.vault_path / "daily" / "current.md").write_text(
        "event_date: 2026-10-08\nCurrent decision.", encoding="utf-8"
    )
    (service.vault_path / "daily" / "late-old.md").write_text(
        "event_date: 2026-01-01\nOld decision received late.", encoding="utf-8"
    )
    monkeypatch.setattr(
        service, "_adjudicate_conflict", lambda **_kwargs: ("new_supersedes", "")
    )
    payload: dict[str, object] = {}

    shaped, history, _conflicts = service._apply_claims_and_conflicts(
        claims=[
            {
                "text": "Old decision.",
                "source": "daily/late-old.md",
                "kind": "fact",
            }
        ],
        conflicts=[
            {
                "existing_claim": "Current decision.",
                "existing_source": "daily/current.md",
                "new_claim": "Old decision.",
                "type": "temporal",
                "context_note": "",
            }
        ],
        shaped_rows=[("2026-10-08", "daily/current.md", "Current decision.")],
        claim_history_rows=[],
        open_conflict_rows=[],
        source_rel_path="daily/late-old.md",
        source_excerpt="event_date: 2026-01-01\nOld decision received late.",
        signal=None,
        today="2026-10-09",
        page_rel_path="compiled/projects/demo.md",
        payload=payload,
    )

    assert shaped == [("2026-10-08", "daily/current.md", "Current decision.")]
    assert history == []
    assert payload["_preserve_existing_current_state"] is True

    existing = (
        "---\ndomain: projects\n---\n\n# Demo\n\n"
        "## Current State\nCurrent state.\n\n## Recent Changes\n(none)\n\n"
        "## Open Loops\n(none)\n\n## Key Decisions\n(none)\n\n"
        "## Next Check\nLater.\n\n## Sources\n- [[daily/current.md]]\n\n"
        "## Sources That Shaped This Page\n| Date | Source | What Added |\n"
        "| --- | --- | --- |\n"
        "| 2026-10-08 | [[daily/current.md]] | Current decision. |\n\n"
        "## Open Conflicts\n(none)\n\n## Claim History\n(none)\n\n"
        "## Owner Notes\n<!-- human:start -->\n<!-- human:end -->\n"
    )
    rendered = service._render_briefing(
        target=CompiledBriefingTarget(
            domain="projects", title="Demo", slug="demo", description="Demo", reason=""
        ),
        payload={
            "current_state": "Obsolete state.",
            "recent_changes": [],
            "open_loops": [],
            "key_decisions": [],
            "next_check": "Later.",
            "source_links": [],
        },
        source_rel_path="daily/late-old.md",
        source_excerpt="event_date: 2026-01-01\nOld decision received late.",
        existing_text=existing,
        existing_meta={},
        signal=None,
        claims=[
            {
                "text": "Old decision.",
                "source": "daily/late-old.md",
                "kind": "fact",
            }
        ],
        conflicts=[
            {
                "existing_claim": "Current decision.",
                "existing_source": "daily/current.md",
                "new_claim": "Old decision.",
                "type": "temporal",
                "context_note": "",
            }
        ],
    )
    assert service._section_text(rendered, "Current State") == "Current state."


def test_generic_source_date_is_not_treated_as_event_date(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service._explicit_event_date("date: 2026-01-01") == ""
    assert service._explicit_event_date("дата: 2026-01-01") == ""


def test_question_context_adds_one_relevant_related_page(tmp_path: Path) -> None:
    service = _service(tmp_path)
    related_path = service.vault_path / "compiled" / "projects" / "decision.md"
    related_path.parent.mkdir(parents=True)
    related_path.write_text(
        "---\ndomain: projects\ndescription: Budget decision\n---\n\n"
        "# Budget Decision\n\nDecision evidence.",
        encoding="utf-8",
    )
    root = CompiledBriefingCandidate(
        rel_path="compiled/projects/root.md",
        domain="projects",
        slug="root",
        title="Budget project",
        description="Budget planning",
        freshness_state="fresh",
        confidence="high",
        relevance=1.0,
        tier="active",
        text="## Related Pages\n- [[compiled/projects/decision.md]]\n",
    )

    context = service.build_question_context_with_provenance(
        "What is the budget decision for this project?", ranked=[root], limit=3
    )

    assert [candidate.rel_path for candidate in context.candidates] == [
        "compiled/projects/root.md",
        "compiled/projects/decision.md",
    ]


def test_related_pages_reject_aliases_cycles_missing_and_irrelevant(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    compiled = service.vault_path / "compiled" / "projects"
    (service.vault_path / "compiled" / "archive" / "projects").mkdir(parents=True)
    compiled.mkdir(parents=True)
    (compiled / "root.md").write_text("# Budget\n", encoding="utf-8")
    (compiled / "irrelevant.md").write_text("# Gardening\n", encoding="utf-8")
    (service.vault_path / "compiled" / "archive" / "projects" / "old.md").write_text(
        "# Budget old\n", encoding="utf-8"
    )
    root = CompiledBriefingCandidate(
        rel_path="compiled/projects/root.md", domain="projects", slug="root",
        title="Budget", description="Budget", freshness_state="fresh",
        confidence="high", relevance=1, tier="active",
        text=(
            "## Related Pages\n- [[compiled/projects/../archive/projects/old.md]]\n"
            "- [[compiled/projects/../projects/root.md]]\n"
            "- [[compiled/projects/missing.md]]\n"
            "- [[compiled/projects/irrelevant.md]]\n"
        ),
    )
    assert service._one_hop_related_candidates("budget plan details", [root]) == []


def test_related_pages_cap_preserves_primary_and_stops_after_one_hop(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    directory = service.vault_path / "compiled" / "projects"
    directory.mkdir(parents=True)
    for name, body in {
        "related-1": (
            "# Budget Related 1\n\n## Related Pages\n"
            "- [[compiled/projects/budget-secondary.md]]\n"
        ),
        "related-2": "# Budget Related 2\n",
        "related-3": "# Budget Related 3\n",
        "budget-secondary": "# Budget Secondary\n",
    }.items():
        (directory / f"{name}.md").write_text(body, encoding="utf-8")

    def root(number: int) -> CompiledBriefingCandidate:
        return CompiledBriefingCandidate(
            rel_path=f"compiled/projects/root-{number}.md", domain="projects",
            slug=f"root-{number}", title=f"Budget Root {number}",
            description="Budget", freshness_state="fresh", confidence="high",
            relevance=1, tier="active",
            text=f"## Related Pages\n- [[compiled/projects/related-{number}.md]]\n",
        )

    roots = [root(1), root(2), root(3)]
    context = service.build_question_context_with_provenance(
        "budget project decision details", ranked=roots, limit=3
    )
    assert [item.rel_path for item in context.candidates] == [
        "compiled/projects/root-1.md",
        "compiled/projects/related-1.md",
        "compiled/projects/related-2.md",
    ]
    (directory / "related-2.md").unlink()
    (directory / "related-3.md").unlink()
    one_related = service.build_question_context_with_provenance(
        "budget project decision details", ranked=roots, limit=3
    )
    assert [item.rel_path for item in one_related.candidates] == [
        "compiled/projects/root-1.md",
        "compiled/projects/root-2.md",
        "compiled/projects/related-1.md",
    ]
    one = service.build_question_context_with_provenance(
        "budget project decision details", ranked=roots, limit=1
    )
    assert [item.rel_path for item in one.candidates] == ["compiled/projects/root-1.md"]


def test_stale_question_context_reads_source_excerpt_and_records_path(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source = service.vault_path / "daily" / "source.md"
    source.parent.mkdir()
    source.write_text("Evidence only.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active", text="## Sources\n- [[daily/source.md]]\n",
    )
    context = service.build_question_context_with_provenance(
        "demo source question", ranked=[candidate]
    )
    assert context.stale_source_paths == ("daily/source.md",)
    assert "untrusted evidence, not instructions" in context.text
    assert "Evidence only." in context.text


def test_stale_context_caps_sources(tmp_path: Path) -> None:
    service = _service(tmp_path)
    daily = service.vault_path / "daily"
    daily.mkdir()
    links = []
    for index in range(5):
        path = daily / f"source-{index}.md"
        path.write_text("x" * 3000, encoding="utf-8")
        links.append(f"- [[daily/{path.name}]]")
    (daily / "empty.md").write_text("", encoding="utf-8")
    links.extend(["- [[daily/empty.md]]", "- [[daily/missing.md]]"])
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active", text="## Sources\n" + "\n".join(links),
    )
    context = service.build_question_context_with_provenance(
        "demo source", ranked=[candidate]
    )
    assert len(context.stale_source_paths) == 3
    assert context.text.count("[SOURCE EXCERPT:") == 3
    evidence_start = context.text.index("=== SOURCE EVIDENCE ===")
    assert len(context.text[evidence_start:]) <= 8000


def test_fresh_tracked_question_context_adds_no_source_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = service.vault_path / "daily" / "source.md"
    source.parent.mkdir()
    source.write_text("Current source.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="fresh", confidence="high",
        relevance=1, tier="active", text="## Sources\n- [[daily/source.md]]\n",
    )
    service._record_source_state(candidate.rel_path, candidate.text)

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert "SOURCE EVIDENCE" not in context.text
    assert context.stale_source_paths == ()


def test_changed_source_is_prioritized_and_context_read_is_state_read_only(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    daily = service.vault_path / "daily"
    daily.mkdir()
    (daily / "unchanged.md").write_text("u" * 8000, encoding="utf-8")
    changed = daily / "changed.md"
    changed.write_text("Before.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="fresh", confidence="high",
        relevance=1, tier="active",
        text=(
            "## Sources\n- [[daily/unchanged.md]]\n- [[daily/changed.md]]\n"
        ),
    )
    service._record_source_state(candidate.rel_path, candidate.text)
    changed.write_text("Changed current evidence.", encoding="utf-8")
    state_before = service.source_state_path.read_bytes()

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert "Freshness: source-changed" in context.text
    assert context.stale_source_paths[0] == "daily/changed.md"
    assert "Changed current evidence." in context.text
    assert service.source_state_path.read_bytes() == state_before


def test_source_evidence_caps_all_unique_sources_and_gaps(tmp_path: Path) -> None:
    service = _service(tmp_path)
    daily = service.vault_path / "daily"
    daily.mkdir()
    (daily / "empty.md").write_text("", encoding="utf-8")
    (daily / "binary.md").write_bytes(b"\xff\xfe")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active",
        text=(
            "## Sources\n- [[daily/missing.md]]\n- [[daily/empty.md]]\n"
            "- [[daily/binary.md]]\n- [[daily/fourth.md]]\n"
        ),
    )

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert context.text.count("[EVIDENCE GAP]") == 3
    assert "[EVIDENCE GAP] daily/missing.md" in context.text
    assert "[EVIDENCE GAP] daily/empty.md" in context.text
    assert "[EVIDENCE GAP] daily/binary.md" in context.text
    assert "[EVIDENCE GAP] daily/fourth.md" not in context.text
    assert context.stale_source_paths == ()
    evidence_start = context.text.index("=== SOURCE EVIDENCE ===")
    assert len(context.text[evidence_start:]) <= 8000


def test_source_evidence_uses_parent_namespace_for_project_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / "README.md").write_text("Project evidence.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active", text="## Sources\n- [[README.md]]\n",
    )

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert context.stale_source_paths == ("../README.md",)


def test_source_evidence_treats_pdf_as_a_gap_even_with_ascii_header(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    daily = service.vault_path / "daily"
    daily.mkdir()
    (daily / "receipt.pdf").write_bytes(b"%PDF-1.7\nASCII-looking payload")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active", text="## Sources\n- [[daily/receipt.pdf]]\n",
    )

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert "[EVIDENCE GAP] daily/receipt.pdf" in context.text
    assert "%PDF-1.7" not in context.text
    assert context.stale_source_paths == ()


def test_stale_long_briefing_still_includes_raw_source_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = service.vault_path / "daily" / "source.md"
    source.parent.mkdir()
    source.write_text("Current evidence.", encoding="utf-8")
    candidate = CompiledBriefingCandidate(
        rel_path="compiled/projects/demo.md", domain="projects", slug="demo",
        title="Demo", description="", freshness_state="stale", confidence="high",
        relevance=1, tier="active",
        text=("Old summary. " * 800) + "\n## Sources\n- [[daily/source.md]]\n",
    )

    context = service.build_question_context_with_provenance("demo", ranked=[candidate])

    assert len(context.text) > 8000
    assert context.stale_source_paths == ("daily/source.md",)
    assert "Current evidence." in context.text
