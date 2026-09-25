"""Tests for ``services/compiled_index.py`` (T3): the ``MOC/compiled-index.md``
catalog of ``compiled/`` pages, grouped by domain.

Uses the real ``write_validated_vault_markdown`` write path against a
temporary vault, the same way ``tests/test_vault_health_scripts.py`` exercises
``vault-health/scripts/generate_moc.py`` for real: a single-level managed
directory (``MOC/``) that ``write_validated_vault_markdown``'s own
``create=True`` parent-creation handles, unlike the CLI-level scenarios
documented in ``tests/test_compiled_fact_check.py``/
``tests/test_compiled_enrich_report.py`` that do need a monkeypatch in this
sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _write_vault_manifest
from test_compiled_briefings import _compiled_service

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_index import refresh_compiled_index
from d_brain.services.frontmatter import (
    parse_frontmatter_bytes,
    read_vault_file_bytes,
    validate_document,
)

INDEX_REL_PATH = Path("MOC/compiled-index.md")


def _compiled_page(
    *,
    title: str | None,
    description: str = "",
    updated: str = "",
    source_count: str = "",
    freshness_state: str = "",
) -> str:
    lines = ["---", "type: compiled-briefing"]
    if description:
        lines.append(f'description: "{description}"')
    if updated:
        lines.append(f"updated: {updated}")
    if source_count:
        lines.append(f"source_count: {source_count}")
    if freshness_state:
        lines.append(f"freshness_state: {freshness_state}")
    lines += ["last_accessed: 2026-07-01", "relevance: 0.8", "tier: active", "---", ""]
    if title is not None:
        lines += [f"# {title}", ""]
    lines.append("Body text.")
    return "\n".join(lines) + "\n"


def _write_compiled_page(
    vault_path: Path, domain: str, slug: str, **kwargs: object
) -> Path:
    path = vault_path / "compiled" / domain / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compiled_page(**kwargs), encoding="utf-8")  # type: ignore[arg-type]
    return path


def _index_bytes(vault_path: Path) -> bytes:
    return read_vault_file_bytes(vault_path, vault_path / INDEX_REL_PATH)


def _index_text(vault_path: Path) -> str:
    return _index_bytes(vault_path).decode("utf-8")


def test_groups_by_known_domain_order_and_sorts_titles_within_domain(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    # Written in reverse domain order and reverse title order, so a passing
    # assertion cannot be an accident of creation/glob order.
    _write_compiled_page(vault_path, "people", "zeta", title="Zeta Person")
    _write_compiled_page(vault_path, "people", "alpha", title="Alpha Person")
    _write_compiled_page(vault_path, "projects", "only", title="Only Project")

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    projects_at = body.index("## projects")
    people_at = body.index("## people")
    alpha_at = body.index("Alpha Person")
    zeta_at = body.index("Zeta Person")
    assert projects_at < people_at, "COMPILED_BRIEFING_DOMAINS order not respected"
    assert alpha_at < zeta_at, "titles within a domain must sort by casefold"


def test_skips_archive_pages(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "kept", title="Kept Page")
    _write_compiled_page(vault_path, "archive", "old", title="Old Archived Page")

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    assert "Kept Page" in body
    assert "Old Archived Page" not in body


def test_empty_fields_are_omitted_without_hanging_separators(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "bare", title="Bare Page")

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    assert "- [[compiled/projects/bare|Bare Page]]\n" in body
    assert "Bare Page]] —" not in body


def test_partial_fields_join_without_leading_or_trailing_separator(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(
        vault_path,
        "projects",
        "partial",
        title="Partial Page",
        updated="2026-08-01",
    )

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    assert (
        "- [[compiled/projects/partial|Partial Page]] — обновлено 2026-08-01\n"
        in body
    )


def test_title_falls_back_to_slug_when_h1_is_missing(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "my-page-name", title=None)

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    assert "[[compiled/projects/my-page-name|my page name]]" in body


def test_unknown_domain_folder_is_appended_after_known_domains(
    tmp_path: Path,
) -> None:
    """A domain folder outside ``COMPILED_BRIEFING_DOMAINS`` (e.g. one added
    on disk before the constant catches up) is not dropped silently -- it
    would simply vanish from the catalog otherwise. It gets its own trailing
    section instead (see the module docstring)."""
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "known", title="Known Page")
    _write_compiled_page(vault_path, "mystery", "stray", title="Stray Page")

    assert refresh_compiled_index(vault_path) is True
    body = _index_text(vault_path)

    assert "## mystery" in body
    assert body.index("## projects") < body.index("## mystery")
    assert "Stray Page" in body


def test_frontmatter_satisfies_the_index_profile(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "only", title="Only Page")

    assert refresh_compiled_index(vault_path) is True
    manifest = load_manifest_for_vault(vault_path)
    document = parse_frontmatter_bytes(_index_bytes(vault_path))

    route, missing, invalid = validate_document(
        INDEX_REL_PATH.as_posix(), document, manifest
    )
    assert route.name == "index"
    assert missing == ()
    assert invalid == ()


def test_second_call_without_changes_does_not_write(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "only", title="Only Page")

    assert refresh_compiled_index(vault_path) is True
    first_bytes = _index_bytes(vault_path)

    assert refresh_compiled_index(vault_path) is False
    assert _index_bytes(vault_path) == first_bytes


def test_changed_page_rewrites_catalog_but_keeps_last_accessed(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _write_compiled_page(vault_path, "projects", "only", title="Only Page")

    assert refresh_compiled_index(vault_path) is True
    first_bytes = _index_bytes(vault_path)
    first_last_accessed = parse_frontmatter_bytes(first_bytes).fields["last_accessed"]

    _write_compiled_page(
        vault_path, "projects", "only", title="Only Page", description="Changed"
    )

    assert refresh_compiled_index(vault_path) is True
    second_bytes = _index_bytes(vault_path)
    second_fields = parse_frontmatter_bytes(second_bytes).fields

    assert second_bytes != first_bytes
    assert second_fields["last_accessed"] == first_last_accessed
    assert "Changed" in second_bytes.decode("utf-8")


def _stub_nightly_maintenance_dependencies(
    monkeypatch: pytest.MonkeyPatch, service: object
) -> None:
    """Isolate ``run_nightly_maintenance`` down to the catalog-refresh call.

    Mirrors ``test_compiled_briefings.py``'s own
    ``test_compiled_briefings_nightly_status_reflects_archival_when_queue_empty``
    stubbing set -- the minimal set that leaves a fresh, empty vault free of
    real compile-enrich work.
    """
    monkeypatch.setattr(
        service,
        "drain_queue",
        lambda **kwargs: {
            "drained": 0,
            "updated": [],
            "consolidations": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(service, "_archive_stale_notes", lambda limit=5: [])
    monkeypatch.setattr(service, "_backfill_freshness_notes", lambda limit=5: [])
    monkeypatch.setattr(service, "lint_notes", lambda: [])
    monkeypatch.setattr(service, "freshness_issues", lambda: [])
    monkeypatch.setattr(service, "_refresh_qmd_index", lambda: None)


def test_run_nightly_maintenance_refreshes_the_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _write_compiled_page(vault_path, "projects", "only", title="Only Page")
    _stub_nightly_maintenance_dependencies(monkeypatch, service)

    result = service.run_nightly_maintenance()

    assert result["compiled_index_written"] is True
    assert "Only Page" in _index_text(vault_path)


def test_run_nightly_maintenance_survives_a_broken_catalog_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the catalog builder must not turn an otherwise fine
    compile-enrich pass into a "failed" one -- the call site wraps it in its
    own try/except precisely so this stays a warning, not a pass failure."""
    vault_path = tmp_path / "vault"
    service = _compiled_service(vault_path)
    _stub_nightly_maintenance_dependencies(monkeypatch, service)

    def _broken_refresh(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "d_brain.services.compiled_index.refresh_compiled_index", _broken_refresh
    )

    result = service.run_nightly_maintenance()

    assert result["compiled_index_written"] is False
    assert result["errors"] == []
    assert not (vault_path / "MOC" / "compiled-index.md").exists()
