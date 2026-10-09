"""Build ``MOC/compiled-index.md``, a domain-grouped catalog of compiled/ pages.

This is a small, read-mostly companion to ``compiled_briefings.py``: it does
not touch any compiled page, only lists them. Domain grouping and order come
straight from ``COMPILED_BRIEFING_DOMAINS`` there instead of a private copy,
so the two never drift apart. A page whose folder name is not one of those
known domains (e.g. a stray ``compiled/<new-domain>/`` created before the
constant was updated) is not dropped silently -- it would simply vanish from
the catalog until someone noticed and fixed the code. Instead it lands in its
own trailing section, folder names sorted alphabetically, so the catalog
still names every page that actually exists on disk.

The rendered body never embeds the current time: the file is only rewritten
through ``write_validated_vault_markdown`` when the candidate bytes actually
differ from what is on disk, so a no-op nightly pass leaves the note (and its
``ops.jsonl``/backup footprint) untouched. The frontmatter's own
``last_accessed``/``relevance``/``tier`` are carried forward from the
existing file for the same reason -- only a first-ever creation stamps
``last_accessed`` with today's date.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.compiled_briefings import (
    COMPILED_BRIEFING_DOMAINS,
    CompiledBriefingService,
)
from d_brain.services.frontmatter import (
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    read_vault_file_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

_CATALOG_RELATIVE_PATH = Path("MOC/compiled-index.md")
_CATALOG_DESCRIPTION = "Каталог страниц compiled/ по доменам для быстрой навигации"
_CATALOG_NOTE = "Генерируется автоматически в ночном проходе, не править вручную."
_DEFAULT_RELEVANCE = 0.9
_DEFAULT_TIER = "active"


@dataclass(frozen=True, slots=True)
class _CatalogPage:
    """One ``compiled/`` page as it will be listed in the catalog."""

    domain: str
    slug: str
    title: str
    description: str
    updated: str
    source_count: str
    freshness_state: str


def _collect_pages(vault_path: Path) -> list[_CatalogPage]:
    """Scan ``compiled/**/*.md`` the same way ``_iter_candidates`` does.

    Not reused directly: that method groups by the ``domain`` *frontmatter*
    field (falling back to the folder name only when the field is absent),
    while the catalog groups by the folder itself, matching the physical
    ``compiled/<domain>/<slug>.md`` layout the archive-skip rule below also
    relies on.
    """
    compiled_root = vault_path / "compiled"
    if not compiled_root.exists():
        return []
    pages: list[_CatalogPage] = []
    for path in sorted(compiled_root.glob("**/*.md")):
        rel_path = path.relative_to(vault_path).as_posix()
        if rel_path.startswith("compiled/archive/"):
            continue
        text = CompiledBriefingService._read_page_text(path)
        fields = CompiledBriefingService._frontmatter_fields(text)
        if fields.get("canonical_path"):
            continue
        title = CompiledBriefingService._title_from_text(text) or path.stem.replace(
            "-", " "
        )
        pages.append(
            _CatalogPage(
                domain=path.parent.name,
                slug=path.stem,
                title=" ".join(title.split()),
                description=" ".join(str(fields.get("description") or "").split()),
                updated=str(fields.get("updated") or "").strip(),
                source_count=str(fields.get("source_count") or "").strip(),
                freshness_state=str(fields.get("freshness_state") or "").strip(),
            )
        )
    return pages


def _page_link(page: _CatalogPage) -> str:
    # A title from the page's own H1 could legally contain "|" or "]]"; both
    # would otherwise break out of the wikilink's display-text slot.
    title = page.title.replace("|", "").replace("]]", "").strip()
    return f"[[compiled/{page.domain}/{page.slug}|{title}]]"


def _render_page_line(page: _CatalogPage) -> str:
    link = _page_link(page)
    segments = [
        page.description,
        f"обновлено {page.updated}" if page.updated else "",
        f"источников {page.source_count}" if page.source_count else "",
        page.freshness_state,
    ]
    tail = " · ".join(segment for segment in segments if segment)
    return f"- {link} — {tail}" if tail else f"- {link}"


def _render_domain_section(domain: str, pages: list[_CatalogPage]) -> list[str]:
    ordered = sorted(pages, key=lambda page: page.title.casefold())
    lines = [f"## {domain}", "", f"Страниц: {len(ordered)}", ""]
    lines.extend(_render_page_line(page) for page in ordered)
    return lines


def write_entity_indexes(
    vault_path: Path,
    *,
    manifest: VaultManifest | None = None,
    existing_lock: VaultWriteLock | None = None,
) -> bool:
    """Maintain primary-card links without replacing the owner's index text."""
    if existing_lock is None:
        with vault_write_lock(vault_path) as lock:
            return write_entity_indexes(
                vault_path, manifest=manifest, existing_lock=lock
            )
    manifest = manifest or load_manifest_for_vault(vault_path)
    changed = False
    pages = _collect_pages(vault_path)
    start = "<!-- d-brain:entities:start -->"
    end = "<!-- d-brain:entities:end -->"
    for domain, rel_path in (
        ("people", "business/network.md"),
        ("projects", "projects/projects.md"),
    ):
        path = vault_path / rel_path
        if not path.exists():
            continue
        old = read_vault_file_bytes(vault_path, path)
        lines = [start, "## Основные карточки", ""]
        groups: dict[str, list[_CatalogPage]] = {}
        for page in pages:
            if page.domain != domain:
                continue
            fields = parse_frontmatter_bytes(
                (vault_path / "compiled" / domain / f"{page.slug}.md").read_bytes()
            ).fields
            names = CompiledBriefingService._normalize_list(fields.get("directions"))
            for name in names or ["Без направления"]:
                groups.setdefault(name, []).append(page)
        for name, entries in sorted(groups.items()):
            lines.extend([f"### {name}", ""])
            lines.extend(
                f"- {_page_link(page)}"
                for page in sorted(entries, key=lambda page: page.title.casefold())
            )
            lines.append("")
        lines.append(end)
        block = "\n".join(lines)
        text = old.decode("utf-8")
        if start in text and end in text:
            left = text.index(start)
            right = text.index(end, left) + len(end)
            text = text[:left] + block + text[right:]
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
        candidate = text.encode("utf-8")
        if candidate == old:
            continue
        write_validated_vault_markdown(
            vault_path,
            path,
            candidate,
            manifest=manifest,
            existing_lock=existing_lock,
            expected_full_sha256=hashlib.sha256(old).hexdigest(),
        )
        changed = True
    return changed


def _render_body(vault_path: Path) -> str:
    by_domain: dict[str, list[_CatalogPage]] = {}
    for page in _collect_pages(vault_path):
        by_domain.setdefault(page.domain, []).append(page)

    known_domains = list(COMPILED_BRIEFING_DOMAINS)
    trailing_domains = sorted(
        domain for domain in by_domain if domain not in known_domains
    )

    lines = ["# Каталог вики", "", _CATALOG_NOTE]
    for domain in known_domains + trailing_domains:
        entries = by_domain.get(domain) or []
        if not entries:
            continue
        lines.append("")
        lines.extend(_render_domain_section(domain, entries))
    lines.append("")
    return "\n".join(lines)


def _render_index_markdown(
    source: bytes | None,
    body: str,
    *,
    last_accessed: object,
    relevance: object,
    tier: object,
) -> bytes:
    """Patch only the managed index fields; the body is fully replaced.

    Byte-construction idiom copied from
    ``vault-health/scripts/generate_moc.py``'s ``_render_moc_markdown``.
    """
    current = source if source is not None else b""
    patched = patch_frontmatter_bytes(
        current,
        {
            "type": "index",
            "description": _CATALOG_DESCRIPTION,
            "last_accessed": last_accessed,
            "relevance": relevance,
            "tier": tier,
        },
    )
    document = parse_frontmatter_bytes(patched)
    if document.header is None:
        raise FrontmatterError("compiled index frontmatter was not generated")
    separator = b"" if document.header.endswith(document.newline) else document.newline
    return (
        b"---"
        + document.newline
        + document.header
        + separator
        + b"---"
        + document.newline
        + body.encode("utf-8")
    )


def refresh_compiled_index(
    vault_path: Path,
    *,
    manifest: VaultManifest | None = None,
) -> bool:
    """Rewrite ``MOC/compiled-index.md`` if its content actually changed.

    Returns ``True`` when the file was written, ``False`` when the existing
    file already matched the freshly rendered candidate byte-for-byte.
    """
    manifest = manifest or load_manifest_for_vault(vault_path)
    index_path = vault_path / _CATALOG_RELATIVE_PATH
    try:
        old_bytes: bytes | None = read_vault_file_bytes(vault_path, index_path)
    except FileNotFoundError:
        old_bytes = None

    old_fields = parse_frontmatter_bytes(old_bytes).fields if old_bytes else {}
    last_accessed = old_fields.get("last_accessed") or date.today().isoformat()
    relevance = old_fields.get("relevance")
    if relevance is None:
        relevance = _DEFAULT_RELEVANCE
    tier = old_fields.get("tier") or _DEFAULT_TIER

    candidate = _render_index_markdown(
        old_bytes,
        _render_body(vault_path),
        last_accessed=last_accessed,
        relevance=relevance,
        tier=tier,
    )
    if candidate == old_bytes:
        return False

    write_validated_vault_markdown(
        vault_path,
        index_path,
        candidate,
        manifest=manifest,
        expected_full_sha256=(
            hashlib.sha256(old_bytes).hexdigest() if old_bytes is not None else None
        ),
        require_absent=old_bytes is None,
    )
    return True
