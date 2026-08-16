"""Smoke tests for the personal-life domains.

Verifies that ``family/``, ``private/``, ``hobbies/`` and ``finances/`` are
wired into the runtime the same way as the upstream domains: present in
``vault-manifest.json`` user_content_roots, declared as qmd collections,
listed in the ``add_descriptions`` frontmatter writer, and seeded into the
distributed ``vault_template`` so a fresh ``a-second-brain init`` recreates
them.

These tests do not assert semantic isolation -- ``private/`` is an ordinary
domain in this fork (single-user vault, OS-level privacy). They only catch
config drift: a future change that drops one of the four domains from any
of these four surfaces will fail here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "src/d_brain/resources"

NEW_DOMAINS: tuple[str, ...] = ("family", "private", "hobbies", "finances")


def _vault_manifest_paths() -> tuple[Path, ...]:
    return (
        REPO_ROOT / "vault-manifest.json",
        TEMPLATE_ROOT / "project_template" / "vault-manifest.json",
    )


@pytest.mark.parametrize("manifest_path", _vault_manifest_paths())
def test_vault_manifest_contains_personal_domains(manifest_path: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    roots = set(payload["user_content_roots"])
    for domain in NEW_DOMAINS:
        assert f"vault/{domain}" in roots, (
            f"{manifest_path.relative_to(REPO_ROOT)}: missing vault/{domain} "
            f"in user_content_roots"
        )


def test_qmd_collections_include_personal_domains() -> None:
    """Verify ``QmdService._collections`` enumerates the four new domains."""
    from d_brain.services.qmd import QmdService

    service = QmdService(REPO_ROOT / "vault")
    names = {name for name, _path, _context in service._collections()}
    for domain in NEW_DOMAINS:
        assert domain in names, f"QmdService._collections missing {domain!r}"


def test_add_descriptions_handles_personal_domains() -> None:
    """The vault-health description writer must not fall through to defaults."""
    import importlib.util

    script_path = (
        TEMPLATE_ROOT
        / "project_template"
        / "skills"
        / "vault-health"
        / "scripts"
        / "add_descriptions.py"
    )
    spec = importlib.util.spec_from_file_location("add_descriptions", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for domain in NEW_DOMAINS:
        description = module.generate_description(
            f"{domain}/example.md", "# Example\n\nA first paragraph.", {}
        )
        assert description is not None, (
            f"add_descriptions returned None for {domain}/"
        )
        assert description, f"add_descriptions returned empty string for {domain}/"


def test_vault_template_seeds_personal_domains() -> None:
    """A fresh ``a-second-brain init`` must recreate the four domain roots."""
    template_vault = TEMPLATE_ROOT / "vault_template"
    for domain in NEW_DOMAINS:
        index_path = template_vault / domain / "_index.md"
        assert index_path.is_file(), (
            f"missing template: {index_path.relative_to(REPO_ROOT)}"
        )
