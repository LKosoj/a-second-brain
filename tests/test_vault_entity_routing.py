"""Entity identity, multi-target capture and editable direction routing."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_compiled_briefings import (
    _compiled_service,
    _demo_target,
    _existing_domain_page,
    _minimal_compile_payload,
)

from d_brain.services.compiled_briefings import BriefingUpsertResult
from d_brain.services.compiled_index import write_entity_indexes
from d_brain.services.frontmatter import parse_frontmatter_bytes


def test_unclear_entity_becomes_question_instead_of_repeated_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _compiled_service(tmp_path)
    source = "imports/plaud/meeting.md"
    (tmp_path / source).parent.mkdir(parents=True)
    (tmp_path / source).write_text("An unnamed participant")
    monkeypatch.setattr(service, "is_available", lambda: True)
    monkeypatch.setattr(service.qmd, "_memory_signal_for_rel_path", lambda _: None)
    monkeypatch.setattr(service, "_resolve_targets", lambda **_: [
        _demo_target(domain="people", title="Игорь", slug="igor"),
        _demo_target(title="Orion", slug="orion"),
    ])
    monkeypatch.setattr(service, "_upsert_briefing", lambda **_: (
        BriefingUpsertResult(path="", written=False, requeueable=True)
    ))
    for _ in range(2):
        result = service.refresh_after_write(source_path=source)
        assert result["unclear_entities"] == ["Игорь", "Orion"]
        assert not result["requeueable"] and not result["updated"]
    queue = json.loads((tmp_path / ".session/decisions-queue.json").read_text())
    assert len(queue) == 1
    assert queue[0]["kind"] == "entity-identity"
    assert queue[0]["page"] == source
    assert "Игорь" in queue[0]["summary"] and "Orion" in queue[0]["summary"]
    assert not (tmp_path / "compiled/people/igor.md").exists()


def test_people_and_projects_do_not_compete_with_other_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _compiled_service(tmp_path)
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects/_index.md").write_text(
        "---\ndirections: [устаревшее]\n---\n# Projects\n"
    )
    (tmp_path / "directions.md").write_text(
        "---\ndirections: [работа, обучение]\n---\n# Направления\n"
    )
    entries = [
        {"domain": "decisions", "title": "Accept a pilot", "slug": "accept-pilot"},
        {"domain": "topics", "title": "An extra topic", "slug": "extra-topic"},
        {
            "domain": "people",
            "title": "Мария Примерова",
            "slug": "maria",
            "directions": ["работа", "invented"],
        },
        {
            "domain": "projects",
            "title": "Orion course",
            "slug": "orion",
            "directions": ["обучение"],
        },
        {"domain": "people", "title": "Иван Тестов", "slug": "ivan"},
    ]
    entries[0]["projects"] = ["compiled/projects/orion.md"]
    monkeypatch.setattr(
        service, "_run_json_dict_prompt", lambda **_: {"updates": entries}
    )
    targets = service._resolve_targets(
        source_rel_path="imports/plaud/meeting.md",
        source_excerpt="Meeting",
        signal=None,
        max_updates=1,
    )
    assert [target.domain for target in targets] == [
        "projects",
        "people",
        "people",
        "decisions",
    ]
    assert targets[1].directions == ("работа",)
    assert targets[-1].projects == ("compiled/projects/orion.md",)
    assert service._configured_directions() == ["работа", "обучение"]
    (tmp_path / "directions.md").write_text(
        "---\ndirections: [семья]\n---\n# Направления\n"
    )
    assert service._configured_directions() == ["семья"]
    directions = [f"направление {number}" for number in range(10)]
    (tmp_path / "directions.md").write_text(
        "---\ndirections: " + json.dumps(directions, ensure_ascii=False)
        + "\n---\n# Направления\n"
    )
    assert service._configured_directions() == directions


def test_alias_reuses_primary_project_without_model_guess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _compiled_service(tmp_path)
    page = _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    page.write_text(
        page.read_text().replace(
            "domain: projects", "domain: projects\naliases: [Орион]"
        )
    )
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda **_: pytest.fail("Exact alias needs no model"),
    )
    target = service._canonical_entity_target(
        _demo_target(title="Орион", slug="new-name"), "Orion renamed"
    )
    assert target is not None
    assert target.existing_path == "compiled/projects/orion.md"
    assert target.title == "Orion"


@pytest.mark.parametrize("outcome", ["unclear", "same", "new"])
def test_identity_resolution_distinguishes_ambiguity_match_and_new_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    service = _compiled_service(tmp_path)
    _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    seen: list[str] = []

    def resolve(**kwargs: object) -> dict[str, str]:
        seen.append(str(kwargs["prompt"]))
        return {"outcome": outcome, "existing_path": "compiled/projects/orion.md"}

    monkeypatch.setattr(service, "_run_json_dict_prompt", resolve)
    target = _demo_target(title="Northern star", slug="northern-star")
    result = service._canonical_entity_target(target, "Source supporting identity")
    if outcome == "unclear":
        assert result is None
        upsert = service._upsert_briefing(
            target=target,
            source_rel_path="daily/2026-10-09.md",
            source_excerpt="Source",
            signal=None,
        )
        assert not upsert.written and upsert.requeueable
        assert not (tmp_path / "compiled/projects/northern-star.md").exists()
    elif outcome == "same":
        assert result and result.existing_path == "compiled/projects/orion.md"
    else:
        assert result == target
    assert "Orion" in seen[0] and "SOURCE_EXCERPT" in seen[0]


def test_render_preserves_directions_aliases_and_links_project(
    tmp_path: Path,
) -> None:
    service = _compiled_service(tmp_path)
    _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    target = _demo_target(
        domain="people",
        title="Мария Примерова",
        slug="maria",
        aliases=("Мария",),
        directions=("обучение",),
        projects=("compiled/projects/orion.md",),
    )
    text = service._render_briefing(
        target=target,
        payload=_minimal_compile_payload(),
        source_rel_path="daily/2026-10-09.md",
        existing_text="",
        existing_meta={},
        signal=None,
    )
    fields = parse_frontmatter_bytes(text.encode()).fields
    assert fields["directions"] == ["обучение"]
    assert fields["aliases"] == ["Мария"]
    assert fields["projects"] == ["compiled/projects/orion.md"]
    assert "[[compiled/projects/orion.md]]" in text
    rewritten = service._render_briefing(
        target=replace(target, directions=(), aliases=(), projects=()),
        payload=_minimal_compile_payload(),
        source_rel_path="daily/2026-10-10.md",
        existing_text=text,
        existing_meta=service._frontmatter_fields(text),
        signal=None,
    )
    assert parse_frontmatter_bytes(rewritten.encode()).fields["directions"] == [
        "обучение"
    ]


def test_entity_indexes_preserve_owner_text_and_hide_historical_duplicates(
    tmp_path: Path,
) -> None:
    service = _compiled_service(tmp_path)
    page = _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    page.write_text(
        page.read_text().replace(
            "domain: projects", "domain: projects\ndirections: [обучение]"
        )
    )
    duplicate = _existing_domain_page(
        tmp_path, "projects", "orion-launch", "Orion launch"
    )
    duplicate.write_text(
        duplicate.read_text().replace(
            "domain: projects",
            "domain: projects\ncanonical_path: compiled/projects/orion.md",
        )
    )
    for path in ["projects/projects.md", "business/network.md"]:
        note = tmp_path / path
        note.parent.mkdir(exist_ok=True)
        note.write_text(
            "---\ntype: note\ndescription: Owner index\n"
            "last_accessed: 2026-10-09\nrelevance: 1.0\ntier: active\n"
            "---\n# Owner index\n\nImportant owner text.\n"
        )
    assert write_entity_indexes(tmp_path)
    text = (tmp_path / "projects/projects.md").read_text()
    assert "Important owner text." in text
    assert "### обучение" in text
    assert "[[compiled/projects/orion|Orion]]" in text
    assert "orion-launch" not in text
    assert not write_entity_indexes(tmp_path)
    assert [item.slug for item in service._iter_candidates()] == ["orion"]


def test_applied_source_can_add_direction_without_recompiling_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _compiled_service(tmp_path)
    target = _demo_target()
    original = service._render_briefing(
        target=target,
        payload=_minimal_compile_payload(),
        source_rel_path="daily/2026-10-09.md",
        existing_text="",
        existing_meta={},
        signal=None,
    )
    page = tmp_path / "compiled/projects/demo-project.md"
    page.parent.mkdir(parents=True)
    page.write_text(original)
    monkeypatch.setattr(service, "_duplicate_source_chunk", lambda **_: True)
    monkeypatch.setattr(
        service,
        "_run_json_dict_prompt",
        lambda **_: pytest.fail("Content is already applied"),
    )
    target = replace(target, directions=("обучение",))
    result = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2026-10-09.md",
        source_excerpt="same source",
        signal=None,
    )
    assert result.written
    after = parse_frontmatter_bytes(page.read_bytes())
    assert after.body == parse_frontmatter_bytes(original.encode()).body
    assert after.fields["directions"] == ["обучение"]
    repeated = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2026-10-09.md",
        source_excerpt="same source",
        signal=None,
    )
    assert not repeated.written


def test_historical_entity_path_redirects_updates_to_primary(tmp_path: Path) -> None:
    service = _compiled_service(tmp_path)
    _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    historical = _existing_domain_page(tmp_path, "projects", "old-orion", "Old Orion")
    historical.write_text(
        historical.read_text().replace(
            "domain: projects",
            "domain: projects\ncanonical_path: compiled/projects/orion.md",
        )
    )
    target = _demo_target(title="Old Orion", slug="old-orion")
    assert service._target_path(target).name == "orion.md"
    resolved = service._canonical_entity_target(target, "Old name in the source")
    assert resolved and resolved.existing_path == "compiled/projects/orion.md"


def test_related_project_is_added_when_source_was_already_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _compiled_service(tmp_path)
    _existing_domain_page(tmp_path, "projects", "orion", "Orion")
    target = _demo_target(domain="people", title="Мария Примерова", slug="maria")
    original = service._render_briefing(
        target=target,
        payload=_minimal_compile_payload(),
        source_rel_path="daily/2026-10-09.md",
        existing_text="",
        existing_meta={},
        signal=None,
    )
    page = tmp_path / "compiled/people/maria.md"
    page.parent.mkdir(parents=True)
    page.write_text(original)
    monkeypatch.setattr(service, "_duplicate_source_chunk", lambda **_: True)
    target = replace(target, projects=("compiled/projects/orion.md",))
    result = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2026-10-09.md",
        source_excerpt="same source",
        signal=None,
    )
    assert result.written
    assert "[[compiled/projects/orion.md]]" in service._section_text(
        page.read_text(), "Related Pages"
    )
    repeated = service._upsert_briefing(
        target=target,
        source_rel_path="daily/2026-10-09.md",
        source_excerpt="same source",
        signal=None,
    )
    assert not repeated.written
