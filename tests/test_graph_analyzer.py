from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT
from conftest import _write_vault_manifest

from d_brain.manifest import ManifestValidationError


def _load_graph_analyzer():
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts/analyze.py"
    spec = importlib.util.spec_from_file_location("graph_analyzer", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_graph_link_builder():
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts/add_links.py"
    spec = importlib.util.spec_from_file_location("graph_link_builder", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_graph_analyzer_resolves_path_links_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "summaries").mkdir()
    (vault_path / "thoughts").mkdir()
    (vault_path / "goals").mkdir()
    (vault_path / "daily").mkdir()
    private_skill = vault_path / "skills/private/local-skill/SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("# Private skill\n", encoding="utf-8")
    (vault_path / "imports" / "plaud" / "notes" / "2026" / "04").mkdir(parents=True)

    (vault_path / "summaries" / "2026-W14-summary.md").write_text(
        "# Summary\n",
        encoding="utf-8",
    )
    (vault_path / "goals" / "1-yearly-2026.md").write_text(
        "# Goals\n",
        encoding="utf-8",
    )
    (vault_path / "MOC" / "MOC-weekly.md").write_text(
        "## Previous Weeks\n\n- [[summaries/2026-W14-summary.md|2026-W14-summary]]\n",
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "retro.md").write_text(
        "[[summaries/2026-W14-summary]]\n[[missing-note]]\n",
        encoding="utf-8",
    )
    # Repository root as the analyzer sees it: vault_path.parent.
    run_agent = tmp_path / "src" / "d_brain" / "run_agent.py"
    run_agent.parent.mkdir(parents=True)
    run_agent.write_text("# entrypoint\n", encoding="utf-8")
    raw_payload = vault_path / "imports/plaud/raw/2026/04/example.json"
    raw_payload.parent.mkdir(parents=True)
    raw_payload.write_text("{}\n", encoding="utf-8")

    (vault_path / "daily" / "2026-04-04.md").write_text(
        "[[vault/goals/1-yearly-2026]]\n"
        "[[src/d_brain/run_agent.py]]\n"
        "[[tests/test_removed_when_published.py]]\n",
        encoding="utf-8",
    )
    (
        vault_path
        / "imports"
        / "plaud"
        / "notes"
        / "2026"
        / "04"
        / "2026-04-03-110103-example.md"
    ).write_text(
        "[[imports/plaud/raw/2026/04/example.json]]\n"
        "`[[imports/plaud/raw/2026/04/already-purged.json]]`\n",
        encoding="utf-8",
    )

    analyzer = _load_graph_analyzer()
    stats = analyzer.analyze_vault(vault_path)
    analyzer.write_artifacts(vault_path, stats)

    assert stats["total_notes"] == 6
    assert "skills/private/local-skill/SKILL" not in stats["notes"]
    assert stats["total_links"] == 3
    # The backticked payload reference is not a link Obsidian would render,
    # so it is never counted -- neither as ignored nor as broken.
    assert stats["total_wikilinks"] == 7
    # A non-note target still on disk stays ignored; one whose file is gone
    # is broken, exactly like a missing note.
    assert stats["ignored_link_count"] == 2
    assert stats["ignored_link_reasons"] == {
        "plaud-raw-payload": 1,
        "repo-path": 1,
    }
    assert stats["broken_link_count"] == 2
    assert sorted(item["target"] for item in stats["broken_links"]) == [
        "missing-note",
        "tests/test_removed_when_published.py",
    ]
    assert stats["links_to"]["summaries/2026-W14-summary"] == [
        "MOC/MOC-weekly",
        "thoughts/retro",
    ]
    assert stats["links_to"]["goals/1-yearly-2026"] == ["daily/2026-04-04"]
    graph_json = vault_path / ".graph" / "vault-graph.json"
    report_md = vault_path / ".graph" / "report.md"
    assert graph_json.exists()
    assert report_md.exists()
    report = report_md.read_text(encoding="utf-8")
    assert "type: technical" in report
    assert "Broken Links" in report
    assert "Ignored Non-note References" in report


def test_broken_links_penalize_the_score_per_link_and_stay_capped(
    tmp_path: Path,
) -> None:
    """Each broken link costs a fixed amount, up to a ceiling.

    As a share of all wikilinks a broken link got cheaper the bigger the
    vault grew -- 52 dead links in a 17568-link vault cost 0.09 -- so the
    score could not distinguish a healthy vault from one the owner had to
    repair by hand.
    """
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "thoughts").mkdir(parents=True)
    first = vault_path / "thoughts" / "first.md"
    second = vault_path / "thoughts" / "second.md"
    body = "---\ndescription: Note\n---\n[[thoughts/{other}]]\n"
    first.write_text(body.format(other="second"), encoding="utf-8")
    second.write_text(body.format(other="first"), encoding="utf-8")

    analyzer = _load_graph_analyzer()
    healthy = analyzer.analyze_vault(vault_path)["health_score"]

    first.write_text(
        body.format(other="second") + "[[thoughts/deleted]]\n", encoding="utf-8"
    )
    one_broken = analyzer.analyze_vault(vault_path)["health_score"]

    assert healthy - one_broken == pytest.approx(analyzer.BROKEN_LINK_PENALTY)

    flood = "".join(f"[[thoughts/gone-{index}]]\n" for index in range(200))
    first.write_text(body.format(other="second") + flood, encoding="utf-8")
    flooded = analyzer.analyze_vault(vault_path)["health_score"]

    assert healthy - flooded == pytest.approx(20.0)


def test_graph_analyzer_probes_dotted_repo_file_names_by_appending_md(
    tmp_path: Path,
) -> None:
    """``[[README.ru]]`` must be probed as ``README.ru.md``, not ``README.md``.

    ``PurePosixPath("README.ru").suffix`` is ``.ru``, so replacing the
    suffix looked for a wholly different file. In this repository that file
    exists, which hid the bug; here only the real target is on disk, so a
    replace-based probe reports the live link broken.
    """
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "daily").mkdir(parents=True)
    (tmp_path / "README.ru.md").write_text("# Readme\n", encoding="utf-8")

    (vault_path / "daily" / "2026-04-04.md").write_text(
        "[[README.ru]]\n", encoding="utf-8"
    )

    stats = _load_graph_analyzer().analyze_vault(vault_path)

    assert stats["broken_link_count"] == 0
    assert stats["ignored_link_reasons"] == {"repo-file": 1}


def test_graph_artifact_writer_requires_manifest_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    analyzer = _load_graph_analyzer()

    with pytest.raises(ManifestValidationError, match="is missing"):
        analyzer.write_artifacts(vault_path, {"health_score": 100})

    assert not (vault_path / ".graph").exists()


def test_graph_analyzer_splits_compiled_domains_and_tracks_archived_isolation(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "compiled" / "projects").mkdir(parents=True)
    (vault_path / "compiled" / "people").mkdir(parents=True)
    (vault_path / "compiled" / "archive" / "projects").mkdir(parents=True)

    (vault_path / "compiled" / "projects" / "acme.md").write_text(
        "# Acme\n\nNo links here.\n", encoding="utf-8"
    )
    (vault_path / "compiled" / "people" / "jane.md").write_text(
        "# Jane\n\nAlso isolated.\n", encoding="utf-8"
    )
    (vault_path / "compiled" / "archive" / "projects" / "old-acme.md").write_text(
        "# Old Acme\n\nArchived and isolated.\n", encoding="utf-8"
    )

    analyzer = _load_graph_analyzer()
    stats = analyzer.analyze_vault(vault_path)

    assert stats["domain_stats"]["compiled/projects"]["count"] == 1
    assert stats["domain_stats"]["compiled/people"]["count"] == 1
    assert stats["domain_stats"]["compiled/archive/projects"]["count"] == 1

    assert "compiled/projects/acme" in stats["orphans"]
    assert "compiled/people/jane" in stats["orphans"]
    assert "compiled/archive/projects/old-acme" not in stats["orphans"]
    assert "compiled/archive/projects/old-acme" not in stats["weakly_connected"]
    assert stats["archived_isolated"] == ["compiled/archive/projects/old-acme"]
    assert stats["archived_isolated_count"] == 1

    report = analyzer.format_report(stats)
    assert "Archived Notes With No Links" in report
    assert "compiled/archive/projects/old-acme" in report


def test_graph_link_builder_routes_thought_categories_to_matching_mocs(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "thoughts" / "ideas").mkdir(parents=True)
    (vault_path / "thoughts" / "learnings").mkdir(parents=True)
    (vault_path / "thoughts" / "reflections").mkdir(parents=True)

    for name in ["MOC-ideas", "MOC-learnings", "MOC-reflections", "MOC-projects"]:
        (vault_path / "MOC" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")

    builder = _load_graph_link_builder()
    mapping = builder.build_moc_mapping(vault_path)

    assert builder.suggest_moc_links(Path("thoughts/ideas/alpha.md"), mapping) == [
        "MOC-ideas"
    ]
    assert builder.suggest_moc_links(
        Path("thoughts/learnings/beta.md"),
        mapping,
    ) == ["MOC-learnings"]
    assert builder.suggest_moc_links(
        Path("thoughts/reflections/gamma.md"),
        mapping,
    ) == ["MOC-reflections"]


def test_graph_link_builder_uses_note_keys_for_duplicate_stem_sources(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "business").mkdir(parents=True)
    (vault_path / "projects").mkdir(parents=True)

    (vault_path / "business" / "_index.md").write_text(
        "# Business\n\nCRM\n",
        encoding="utf-8",
    )
    (vault_path / "projects" / "_index.md").write_text(
        "# Projects\n\nClients\n",
        encoding="utf-8",
    )
    (vault_path / "business" / "crm.md").write_text("# CRM\n", encoding="utf-8")
    (vault_path / "projects" / "clients.md").write_text("# Clients\n", encoding="utf-8")

    builder = _load_graph_link_builder()
    suggestions = builder.analyze_and_suggest(vault_path)

    assert "business/_index" in suggestions
    assert "projects/_index" in suggestions
    assert "_index" not in suggestions


def _compiled_page_with_human_related(tmp_path: Path) -> Path:
    """A compiled page whose only "## Related" heading sits inside the
    owner's human zone -- the case a blind insert would corrupt."""
    page = tmp_path / "aurora.md"
    page.write_text(
        "# Aurora\n\n"
        "## Current State\n\nIdle.\n\n"
        "<!-- human:start -->\n"
        "## Owner Notes\n\n"
        "## Related\n\n- [[projects/borealis]]\n"
        "<!-- human:end -->\n",
        encoding="utf-8",
    )
    return page


def test_graph_link_builder_never_writes_inside_the_human_zone(tmp_path: Path) -> None:
    """The human zone survives every pass verbatim; appending under a
    "## Related" heading that happens to live inside it would rewrite text
    the owner typed by hand."""
    builder = _load_graph_link_builder()
    page = _compiled_page_with_human_related(tmp_path)

    assert builder.apply_link(page, "topics/quantum-widgets", dry_run=False) is True

    content = page.read_text(encoding="utf-8")
    start = content.index("<!-- human:start -->")
    end = content.index("<!-- human:end -->")
    assert "[[topics/quantum-widgets]]" not in content[start:end]
    assert "[[topics/quantum-widgets]]" in content[end:]


def test_graph_link_builder_keeps_human_zone_bytes_including_line_endings(
    tmp_path: Path,
) -> None:
    """"Verbatim" is about bytes, not about how the text looks.

    ``apply_link`` rewrites the whole file, so reading it with translated
    line endings quietly rewrote every CRLF the owner had typed inside
    their own zone -- the note read the same and hashed differently.
    """
    builder = _load_graph_link_builder()
    page = tmp_path / "aurora.md"
    zone = (
        b"<!-- human:start -->\r\n"
        b"\xd0\x9c\xd0\xbe\xd1\x8f \xd0\xb7\xd0\xb0\xd0\xbc\xd0\xb5\xd1\x82\xd0\xba"
        b"\xd0\xb0.\r\n"
        b"<!-- human:end -->"
    )
    page.write_bytes(b"# Aurora\n\n## Owner Notes\n" + zone + b"\n")

    assert builder.apply_link(page, "topics/quantum-widgets", dry_run=False) is True

    after = page.read_bytes()
    assert zone in after
    assert b"[[topics/quantum-widgets]]" in after


def test_graph_link_builder_skips_a_note_that_is_not_valid_utf8(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One undecodable note must not take the whole run down with it.

    ``analyze_and_suggest`` reads with ``errors="ignore"``, so such a note
    still produces suggestions and reaches ``apply_link``. A strict decode
    there raised out of ``main``'s loop, so every note queued behind it
    silently never got its links -- on that run and on every rerun, until
    someone found and fixed the bad note by hand. Decoding leniently would
    be worse still: this function rewrites the whole file, so it would
    write the replacement characters back over the real bytes.
    """
    builder = _load_graph_link_builder()
    page = tmp_path / "aurora.md"
    original = b"# Aurora\n\nOwner note: \xff\xfe\n"
    page.write_bytes(original)

    assert builder.apply_link(page, "topics/quantum-widgets", dry_run=False) is False

    assert page.read_bytes() == original
    assert "[SKIP]" in capsys.readouterr().out


def test_graph_link_builder_leaves_ambiguous_human_zone_untouched(
    tmp_path: Path,
) -> None:
    """Two zones mean no safe pairing of start/end markers -- fail closed
    rather than guess, same as vault-health's fix_links.py."""
    builder = _load_graph_link_builder()
    page = tmp_path / "aurora.md"
    original = (
        "# Aurora\n\n"
        "<!-- human:start -->\nfirst\n<!-- human:end -->\n\n"
        "<!-- human:start -->\nsecond\n<!-- human:end -->\n"
    )
    page.write_text(original, encoding="utf-8")

    assert builder.apply_link(page, "topics/quantum-widgets", dry_run=False) is False
    assert page.read_text(encoding="utf-8") == original


def test_graph_link_builder_leaves_unpaired_marker_human_zone_untouched(
    tmp_path: Path,
) -> None:
    """A lone START with no matching END is exactly as ambiguous as two
    zones -- there's no way to tell where the zone would have ended, so
    fail closed rather than treat the "## Related" heading inside it as an
    ordinary insertion point."""
    builder = _load_graph_link_builder()
    page = tmp_path / "aurora.md"
    original = (
        "# Aurora\n\n"
        "<!-- human:start -->\n"
        "## Owner Notes\n\n"
        "## Related\n\n- [[projects/borealis]]\n"
    )
    page.write_text(original, encoding="utf-8")

    assert builder.apply_link(page, "topics/quantum-widgets", dry_run=False) is False
    assert page.read_text(encoding="utf-8") == original


def test_graph_link_builder_human_zone_span_unpaired_marker_is_ambiguous() -> None:
    builder = _load_graph_link_builder()

    assert (
        builder.human_zone_span(f"before {builder.HUMAN_ZONE_START} after")
        == builder.AMBIGUOUS_HUMAN_ZONE
    )
    assert (
        builder.human_zone_span(f"before {builder.HUMAN_ZONE_END} after")
        == builder.AMBIGUOUS_HUMAN_ZONE
    )
    assert builder.human_zone_span("no markers here") is None


def test_graph_link_builder_human_zone_span_reversed_pair_is_ambiguous() -> None:
    """One START and one END, but the END comes first: the counts alone say
    "well-formed pair", and taking the span at face value would hand back a
    negative range -- the script would then treat the owner's notes as *not*
    protected and could write straight into them."""
    builder = _load_graph_link_builder()
    reversed_pair = (
        f"# Aurora\n\n{builder.HUMAN_ZONE_END}\n"
        f"## Owner Notes\n\n{builder.HUMAN_ZONE_START}\n"
    )

    assert builder.human_zone_span(reversed_pair) == builder.AMBIGUOUS_HUMAN_ZONE


def test_graph_link_builder_human_zone_span_zero_markers_with_owner_notes_heading() -> (
    None
):
    """Zero exact markers is only "no zone yet" on a page that never had one.
    A page still carrying the ``## Owner Notes`` heading that compiled
    briefings always render together with the markers has lost them to some
    external edit -- writing into that file would land inside what used to
    be the owner's own text."""
    builder = _load_graph_link_builder()
    stripped = "# Aurora\n\n## Owner Notes\n\nМои заметки.\n"

    assert builder.human_zone_span(stripped) == builder.AMBIGUOUS_HUMAN_ZONE
