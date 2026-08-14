#!/usr/bin/env python3
"""
Fix broken wiki-links for the current flat vault layout.

Supported repairs:
1. strip trailing backslashes from broken targets
2. collapse legacy nested business/project paths to current flat notes
3. resolve unique stem matches
4. remove links to internal repo paths, attachments, and stale aliases
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
VAULT_PATH = PROJECT_ROOT / "vault"
GRAPH_PATH = VAULT_PATH / ".graph" / "vault-graph.json"
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from d_brain.manifest import VaultManifest, load_manifest_for_vault  # noqa: E402
from d_brain.services.compiled_briefings import (  # noqa: E402
    HUMAN_ZONE_END,
    HUMAN_ZONE_START,
    human_zone_markers_look_corrupted,
)
from d_brain.services.frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_frontmatter_bytes,
    read_vault_file_bytes,
    validate_document,
    write_validated_vault_markdown,
)

IGNORE_DIRS = {".obsidian", "attachments", ".git", ".graph", ".claude", ".trash", "skills"}
EXCLUDE_PATTERNS = {"backup", ".backup", "business.backup"}
LEGACY_PREFIX_REWRITES = {
    "business/crm/": "business/crm",
    "business/network/": "business/network",
    "business/events/": "business/events",
    "business/projects/": "projects/projects",
    "projects/clients/": "projects/clients",
    "projects/leads/": "projects/leads",
    "projects/projects/": "projects/projects",
}
REMOVABLE_PREFIXES = (".claude/", "vault/", "scripts/", "../", "~/", "attachments/")


def load_graph() -> dict:
    if not GRAPH_PATH.exists():
        print("Error: vault-graph.json not found. Run analyze.py first.")
        sys.exit(1)
    return json.loads(GRAPH_PATH.read_text(encoding="utf-8"))


def is_indexable_note(rel_path: Path) -> bool:
    """Whether one vault-relative note may be offered as a repair target.

    ``graph-builder/scripts/analyze.py`` skips every hidden directory
    outright when it walks the same vault. Naming them one by one here
    instead let ``.session`` through, and
    ``.session/compile-enrich/snapshots/<hash>/blobs/compiled/**`` keeps a
    verbatim copy of every compiled page a nightly pass was about to
    rewrite. For a page since deleted from ``compiled/``, that copy is the
    only file left carrying its stem -- so ``_unique_stem_match`` reads as
    unique and the repair retargets the owner's link into a scratch cache
    ``SNAPSHOT_RETENTION_DAYS`` deletes 14 days later. analyze.py then
    classifies such a target as ``hidden-path`` and never reports it, so
    nothing downstream could notice the damage either.

    Matching is on the vault-relative path, not the absolute one: whether a
    note is internal is a property of where it sits inside the vault, and
    an absolute match also fires on any directory above the vault that
    happens to be named like one of these.
    """
    parts = rel_path.parts
    if any(part.startswith(".") for part in parts):
        return False
    if any(part in IGNORE_DIRS for part in parts):
        return False
    return not any(pattern in rel_path.as_posix() for pattern in EXCLUDE_PATTERNS)


def build_stem_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    md_files = [
        file_path
        for file_path in VAULT_PATH.rglob("*.md")
        if is_indexable_note(file_path.relative_to(VAULT_PATH))
    ]
    for md_file in md_files:
        rel = str(md_file.relative_to(VAULT_PATH))
        stem = md_file.stem
        index[stem].append(rel)
        index[rel.removesuffix(".md")].append(rel)
    return index


def _existing_note(target: str) -> str | None:
    candidate = VAULT_PATH / f"{target}.md"
    if candidate.exists():
        return target
    return None


def _unique_stem_match(target: str, stem_index: dict[str, list[str]]) -> str | None:
    stem = Path(target).stem
    matches = stem_index.get(stem, [])
    if len(matches) == 1:
        return matches[0].removesuffix(".md")
    return None


def _rewrite_legacy_prefix(target: str) -> str | None:
    for prefix, replacement in LEGACY_PREFIX_REWRITES.items():
        if target.startswith(prefix):
            return replacement
    return None


def suggest_fix(
    broken_from: str,
    broken_to: str,
    stem_index: dict[str, list[str]],
) -> tuple[str | None, str]:
    target = broken_to.rstrip("\\")

    if any(target.startswith(prefix) for prefix in REMOVABLE_PREFIXES):
        return None, "remove"

    if re.match(r"3-weekly-\d{4}-W\d+", target):
        return None, "remove"

    if "/" not in target and target[:1].isupper() and broken_from.startswith(
        ("business/", "projects/")
    ):
        return None, "remove"

    existing = _existing_note(target)
    if existing is not None:
        return existing, "replace"

    rewritten = _rewrite_legacy_prefix(target)
    if rewritten is not None and _existing_note(rewritten) is not None:
        return rewritten, "replace"

    unique = _unique_stem_match(target, stem_index)
    if unique is not None:
        return unique, "replace"

    if "/" in target and _existing_note(target) is None:
        return None, "remove"

    return None, "none"


def _protect_human_zone(content: str, transform) -> str:
    """Apply `transform` everywhere except inside the owner's human zone.

    Compiled briefings (see ``services/compiled_briefings.py``) keep one
    ``## Owner Notes`` section wrapped in ``HUMAN_ZONE_START``/
    ``HUMAN_ZONE_END`` HTML comments; that block survives every
    recompilation, archival, and link-repair pass verbatim -- including
    this one. A blind whole-file regex substitution here would silently
    rewrite text the owner typed by hand, so the single well-ordered zone
    (its one START through its matching END, inclusive) is carved out and
    left byte-for-byte untouched; `transform` only ever sees the text
    before and after it.

    fix_links.py walks the whole vault, not just compiled/ pages, so most
    files have no markers at all -- that case has nothing to protect, and
    `transform` runs over the whole file.

    Any other marker count is fail-closed rather than guessed at: two or
    more of either marker, a single START with no matching END, a single
    END with no matching START, a single pair in reversed order, or zero
    exact markers alongside either of the two independent signals
    ``human_zone_markers_look_corrupted`` checks (code review defect 1): the
    ``## Owner Notes`` heading compiled briefings always render together
    with the markers, or the page's ``human_zone_populated`` frontmatter
    flag, set once the zone has ever held real text and never cleared --
    either surviving without the markers means they were corrupted since,
    however that corruption looks -- all mean which START pairs with which
    END (or whether there is a real pair at all) can't be told apart from a
    lone marker the owner left behind while editing. Guessing wrong is
    exactly the silent-corruption bug this function exists to prevent, so
    every one of these cases leaves the file completely untouched and the
    caller's transform does not run at all -- mirroring
    ``compiled_briefings._extract_human_zone``'s ``HumanZoneMarkerError``
    and ``add_links.py::human_zone_span``'s ``AMBIGUOUS_HUMAN_ZONE`` for
    the same ambiguity.
    """
    starts = content.count(HUMAN_ZONE_START)
    ends = content.count(HUMAN_ZONE_END)

    if starts == 0 and ends == 0:
        if human_zone_markers_look_corrupted(content):
            return content
        return transform(content)

    if starts != 1 or ends != 1:
        return content

    start_index = content.find(HUMAN_ZONE_START)
    end_index = content.find(HUMAN_ZONE_END)
    if end_index < start_index:
        return content

    end_index += len(HUMAN_ZONE_END)
    before = transform(content[:start_index])
    zone = content[start_index:end_index]
    after = transform(content[end_index:])
    return before + zone + after


def _write_repair(
    vault_path: Path,
    file_path: Path,
    transform,
    *,
    manifest: VaultManifest,
) -> bool:
    source = read_vault_file_bytes(vault_path, file_path)
    content = source.decode("utf-8")
    new_content = _protect_human_zone(content, transform)
    if new_content == content:
        return False
    candidate = new_content.encode("utf-8")
    document = parse_frontmatter_bytes(candidate)
    _route, missing, invalid = validate_document(
        file_path.as_posix(), document, manifest
    )
    if missing or invalid:
        raise FrontmatterError(
            "generated frontmatter is invalid: "
            f"missing={','.join(missing) or '-'} "
            f"invalid={','.join(invalid) or '-'}"
        )
    write_validated_vault_markdown(
        vault_path,
        file_path,
        candidate,
        manifest=manifest,
        expected_full_sha256=hashlib.sha256(source).hexdigest(),
    )
    return True


def apply_replace(
    vault_path: Path,
    file_path: Path,
    old_link: str,
    new_link: str,
    *,
    manifest: VaultManifest,
) -> bool:
    escaped = re.escape(old_link)
    pattern = rf"\[\[{escaped}(\\?[|\]#])"
    return _write_repair(
        vault_path,
        file_path,
        lambda content: re.sub(pattern, f"[[{new_link}\\1", content),
        manifest=manifest,
    )


def apply_remove(
    vault_path: Path,
    file_path: Path,
    old_link: str,
    *,
    manifest: VaultManifest,
) -> bool:
    escaped = re.escape(old_link)
    return _write_repair(
        vault_path,
        file_path,
        lambda content: re.sub(
            rf"\[\[{escaped}\]\]",
            old_link,
            re.sub(rf"\[\[{escaped}\|([^\]]+)\]\]", r"\1", content),
        ),
        manifest=manifest,
    )


def _is_cas_conflict(error: BaseException) -> bool:
    return "expected_full_sha256" in str(error)


def fix_targets(
    vault_path: Path,
    graph: dict,
    *,
    apply: bool,
    manifest: VaultManifest | None = None,
    stem_index: dict[str, list[str]] | None = None,
) -> dict[str, int]:
    manifest = manifest or load_manifest_for_vault(vault_path)
    if stem_index is None:
        stem_index = build_stem_index()
    stats = {"fixed": 0, "removed": 0, "skipped": 0, "conflicts": 0, "errors": 0}

    for item in graph.get("broken_links", []):
        source = item["source"]
        target = item["target"]
        suggestion, action = suggest_fix(source, target, stem_index)
        source_path = Path(f"{source}.md")

        if action == "replace" and suggestion is not None:
            print(f"  REPLACE {source}: [[{target}]] -> [[{suggestion}]]")
            if not apply:
                stats["fixed"] += 1
                continue
            try:
                if apply_replace(
                    vault_path,
                    source_path,
                    target,
                    suggestion,
                    manifest=manifest,
                ):
                    stats["fixed"] += 1
            except (FrontmatterError, OSError, UnicodeDecodeError) as exc:
                key = "conflicts" if _is_cas_conflict(exc) else "errors"
                stats[key] += 1
                label = "ERROR CONCURRENT" if key == "conflicts" else "ERROR"
                print(f"  {label} {source}: {exc}")
        elif action == "remove":
            print(f"  REMOVE  {source}: [[{target}]]")
            if not apply:
                stats["removed"] += 1
                continue
            try:
                if apply_remove(
                    vault_path,
                    source_path,
                    target,
                    manifest=manifest,
                ):
                    stats["removed"] += 1
            except (FrontmatterError, OSError, UnicodeDecodeError) as exc:
                key = "conflicts" if _is_cas_conflict(exc) else "errors"
                stats[key] += 1
                label = "ERROR CONCURRENT" if key == "conflicts" else "ERROR"
                print(f"  {label} {source}: {exc}")
        else:
            print(f"  SKIP    {source}: [[{target}]]")
            stats["skipped"] += 1

    return stats


def main() -> int:
    apply = "--apply" in sys.argv
    graph = load_graph()
    stem_index = build_stem_index()
    manifest = load_manifest_for_vault(VAULT_PATH)

    print(f"Broken links: {len(graph.get('broken_links', []))}")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print()

    stats = fix_targets(
        VAULT_PATH,
        graph,
        apply=apply,
        manifest=manifest,
        stem_index=stem_index,
    )
    print(f"\nFixed: {stats['fixed']}")
    print(f"Removed: {stats['removed']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Concurrent conflicts: {stats['conflicts']}")
    print(f"Errors: {stats['errors']}")
    return 1 if stats["conflicts"] or stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
