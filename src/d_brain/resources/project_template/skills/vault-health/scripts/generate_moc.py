#!/usr/bin/env python3
"""
Refresh the managed thought MOCs for the current flat vault layout.

Managed files:
  - vault/MOC/MOC-ideas.md
  - vault/MOC/MOC-learnings.md
  - vault/MOC/MOC-reflections.md
  - vault/MOC/MOC-projects.md

This script intentionally does not generate legacy `MOC-business.md` or touch
manually curated files like `MOC/index.md`, `MOC/MOC-weekly.md`, or
`MOC/MOC-plaud.md`.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
VAULT_PATH = PROJECT_ROOT / "vault"
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from d_brain.manifest import VaultManifest, load_manifest_for_vault  # noqa: E402
from d_brain.services.frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    read_vault_file_bytes,
    read_vault_file_text,
    write_validated_vault_markdown,
)

MANAGED_MOCS = {
    "ideas": {
        "path": "MOC-ideas.md",
        "title": "Ideas",
        "heading": "Recent",
        "empty": "<!-- Recent ideas will be added here -->",
    },
    "learnings": {
        "path": "MOC-learnings.md",
        "title": "Learnings",
        "heading": "Recent",
        "empty": "<!-- New learnings will appear here -->",
    },
    "reflections": {
        "path": "MOC-reflections.md",
        "title": "Reflections",
        "heading": "Recent",
        "empty": "<!-- Recent reflections will appear here -->",
    },
    "projects": {
        "path": "MOC-projects.md",
        "title": "Projects",
        "heading": "Active",
        "empty": "<!-- Active project thoughts -->",
    },
}


def _extract_title(vault_path: Path, note_path: Path) -> str:
    content = read_vault_file_text(vault_path, note_path)
    match = re.search(r"^# (.+)$", content, re.MULTILINE)
    if match is not None:
        return match.group(1).strip()
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", note_path.stem)
    return stem.replace("-", " ").strip().title()


def _render_links(vault_path: Path, notes_dir: Path) -> list[str]:
    note_paths = sorted(notes_dir.glob("*.md"), reverse=True)
    lines: list[str] = []
    for note_path in note_paths:
        rel = note_path.relative_to(vault_path).with_suffix("").as_posix()
        lines.append(f"- [[{rel}|{_extract_title(vault_path, note_path)}]]")
    return lines


def _split_existing_sections(
    existing_body: str,
    section_heading: str,
) -> tuple[str, str]:
    lines = existing_body.splitlines()
    if not lines or not lines[0].startswith("# "):
        return "", ""

    managed_heading = f"## {section_heading}"
    intro_lines: list[str] = []
    tail_lines: list[str] = []
    index = 1

    while index < len(lines) and lines[index] != managed_heading:
        intro_lines.append(lines[index])
        index += 1

    if index < len(lines):
        index += 1
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1
        tail_lines = lines[index:]

    intro = "\n".join(intro_lines).strip()
    tail = "\n".join(tail_lines).strip()
    return intro, tail


def _render_body(
    title: str,
    intro: str,
    section_heading: str,
    link_lines: list[str],
    empty: str,
    tail: str,
) -> str:
    lines = [f"# {title}", ""]
    if intro:
        lines.append(intro)
        lines.append("")

    lines.extend([f"## {section_heading}", ""])
    if link_lines:
        lines.extend(link_lines)
    else:
        lines.append(empty)

    if tail:
        lines.extend(["", tail])
    lines.append("")
    return "\n".join(lines)


def _render_moc_markdown(
    source: bytes | None,
    body: str,
    *,
    title: str,
) -> bytes:
    """Patch only managed index fields while replacing the managed MOC body."""
    current = source if source is not None else b""
    patched = patch_frontmatter_bytes(
        current,
        {
            "type": "index",
            "description": f"Map of Content: {title}",
            "last_accessed": date.today().isoformat(),
            "relevance": 1.0,
            "tier": "active",
        },
    )
    document = parse_frontmatter_bytes(patched)
    if document.header is None:
        raise FrontmatterError("MOC frontmatter was not generated")
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


def refresh_thought_mocs(
    vault_path: Path,
    *,
    manifest: VaultManifest | None = None,
) -> list[Path]:
    manifest = manifest or load_manifest_for_vault(vault_path)
    moc_dir = vault_path / "MOC"
    written_paths: list[Path] = []

    for category, config in MANAGED_MOCS.items():
        moc_path = moc_dir / config["path"]
        source: bytes | None = None
        intro = ""
        tail = ""
        try:
            source = read_vault_file_bytes(vault_path, moc_path)
        except FileNotFoundError:
            source = None
        if source is not None:
            existing_body = parse_frontmatter_bytes(source).body.decode("utf-8")
            intro, tail = _split_existing_sections(
                existing_body,
                config["heading"],
            )

        notes_dir = vault_path / "thoughts" / category
        links = _render_links(vault_path, notes_dir) if notes_dir.exists() else []
        body = _render_body(
            config["title"],
            intro,
            config["heading"],
            links,
            config["empty"],
            tail,
        )
        candidate = _render_moc_markdown(source, body, title=config["title"])
        if candidate != source:
            write_validated_vault_markdown(
                vault_path,
                moc_path,
                candidate,
                manifest=manifest,
                expected_full_sha256=(
                    hashlib.sha256(source).hexdigest()
                    if source is not None
                    else None
                ),
                require_absent=source is None,
            )
        written_paths.append(moc_path)

    return written_paths


def main() -> None:
    manifest = load_manifest_for_vault(VAULT_PATH)
    for path in refresh_thought_mocs(VAULT_PATH, manifest=manifest):
        print(f"Generated: {path.relative_to(VAULT_PATH)}")


if __name__ == "__main__":
    main()
