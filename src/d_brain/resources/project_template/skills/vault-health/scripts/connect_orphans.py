#!/usr/bin/env python3
"""
Connect orphan and weakly-connected notes to the current hub notes.

This script is intentionally conservative:
- skips internal/hidden/runtime folders
- skips daily notes
- adds one hub link only when `related` is missing
- uses the current flat vault layout and current MOC set
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
VAULT_PATH = PROJECT_ROOT / "vault"
GRAPH_PATH = VAULT_PATH / ".graph" / "vault-graph.json"
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from d_brain.manifest import VaultManifest, load_manifest_for_vault  # noqa: E402
from d_brain.services.frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    read_vault_file_bytes,
    validate_document,
    write_validated_vault_markdown,
)

HUB_MAP = [
    ("imports/plaud/notes/", '[[MOC/MOC-plaud]]'),
    ("summaries/", '[[MOC/MOC-weekly]]'),
    ("thoughts/ideas/", '[[MOC/MOC-ideas]]'),
    ("thoughts/learnings/", '[[MOC/MOC-learnings]]'),
    ("thoughts/reflections/", '[[MOC/MOC-reflections]]'),
    ("thoughts/projects/", '[[MOC/MOC-projects]]'),
    ("thoughts/", '[[MEMORY]]'),
    ("business/", '[[business/_index]]'),
    ("projects/", '[[projects/_index]]'),
    ("goals/", '[[MEMORY]]'),
    ("MOC/", '[[MOC/index]]'),
]

SKIP_PREFIXES = ("daily/", "templates/", ".claude/", ".graph/", ".session/")


def load_graph(graph_path: Path) -> dict:
    if not graph_path.exists():
        print("Error: vault-graph.json not found. Run analyze.py first.")
        sys.exit(1)
    return json.loads(graph_path.read_text(encoding="utf-8"))


def get_hub_for_path(rel_path: str) -> str | None:
    if any(rel_path.startswith(prefix) for prefix in SKIP_PREFIXES):
        return None
    for prefix, hub in HUB_MAP:
        if rel_path.startswith(prefix):
            return hub
    return "[[MEMORY]]"


def has_related_field(content: str) -> bool:
    return "related" in parse_frontmatter_bytes(content.encode("utf-8")).fields


def add_related_to_frontmatter(content: str, hub_link: str) -> str:
    if has_related_field(content):
        return content
    return patch_frontmatter_bytes(
        content.encode("utf-8"), {"related": [hub_link]}
    ).decode("utf-8")


def _read_note(vault_path: Path, rel_path: str) -> tuple[Path, bytes] | None:
    direct = Path(rel_path)
    candidates = (direct,) if direct.suffix else (direct, direct.with_suffix(".md"))
    for candidate in candidates:
        try:
            return candidate, read_vault_file_bytes(vault_path, candidate)
        except FileNotFoundError:
            continue
    return None


def _is_cas_conflict(error: BaseException) -> bool:
    return "expected_full_sha256" in str(error)


def connect_targets(
    vault_path: Path,
    graph: dict,
    *,
    apply: bool,
    manifest: VaultManifest | None = None,
) -> dict[str, int]:
    manifest = manifest or load_manifest_for_vault(vault_path)
    orphans = graph.get("orphans", [])
    weakly_connected = graph.get("weakly_connected", [])
    targets = set(orphans + weakly_connected)

    stats = {
        "connected": 0,
        "skipped": 0,
        "already_has": 0,
        "missing": 0,
        "conflicts": 0,
        "errors": 0,
    }

    for rel_path in sorted(targets):
        hub_link = get_hub_for_path(rel_path)
        if hub_link is None:
            stats["skipped"] += 1
            continue

        try:
            resolved = _read_note(vault_path, rel_path)
        except (FrontmatterError, OSError, UnicodeDecodeError) as exc:
            stats["errors"] += 1
            print(f"ERROR: {rel_path}: {exc}")
            continue
        if resolved is None:
            stats["missing"] += 1
            continue
        file_path, source = resolved
        try:
            content = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            stats["errors"] += 1
            print(f"ERROR: {rel_path}: {exc}")
            continue
        try:
            if has_related_field(content):
                stats["already_has"] += 1
                continue

            new_content = add_related_to_frontmatter(content, hub_link)
            if new_content == content:
                continue

            candidate = new_content.encode("utf-8")
            document = parse_frontmatter_bytes(candidate)
            _route, missing, invalid = validate_document(
                file_path.as_posix(), document, manifest
            )
        except FrontmatterError as exc:
            stats["errors"] += 1
            print(f"ERROR: {rel_path}: {exc}")
            continue
        if missing or invalid:
            stats["errors"] += 1
            print(
                f"ERROR: {rel_path}: generated frontmatter is invalid "
                f"missing={','.join(missing) or '-'} "
                f"invalid={','.join(invalid) or '-'}"
            )
            continue
        if apply:
            try:
                write_validated_vault_markdown(
                    vault_path,
                    file_path,
                    candidate,
                    manifest=manifest,
                    expected_full_sha256=hashlib.sha256(source).hexdigest(),
                )
            except (FrontmatterError, OSError) as exc:
                key = "conflicts" if _is_cas_conflict(exc) else "errors"
                stats[key] += 1
                label = "ERROR CONCURRENT" if key == "conflicts" else "ERROR"
                print(f"{label}: {rel_path}: {exc}")
                continue
        stats["connected"] += 1

    return stats


def main() -> int:
    apply = "--apply" in sys.argv
    graph = load_graph(GRAPH_PATH)
    manifest = load_manifest_for_vault(VAULT_PATH)
    stats = connect_targets(VAULT_PATH, graph, apply=apply, manifest=manifest)

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Connected: {stats['connected']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Already has related: {stats['already_has']}")
    print(f"Missing files: {stats['missing']}")
    print(f"Concurrent conflicts: {stats['conflicts']}")
    print(f"Errors: {stats['errors']}")
    return 1 if stats["conflicts"] or stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
