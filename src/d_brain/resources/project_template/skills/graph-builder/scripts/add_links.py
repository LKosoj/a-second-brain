#!/usr/bin/env python3
"""
Link Builder for Obsidian Vault

Suggests and adds wiki-links based on content analysis.
Run with: uv run add_links.py [vault_path] [--dry-run]
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path


def note_key(vault_path: Path, note_path: Path) -> str:
    """Normalize a note path to a vault-relative wikilink target."""
    return note_path.relative_to(vault_path).with_suffix("").as_posix()


def extract_existing_links(content: str) -> set[str]:
    """Extract existing [[wiki-links]] from content."""
    pattern = r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]'
    return set(re.findall(pattern, content))


def find_mentions(content: str, note_titles: set[str]) -> list[tuple[str, int]]:
    """Find mentions of note titles in content without existing links."""
    existing = extract_existing_links(content)
    mentions = []

    for title in note_titles:
        if title in existing:
            continue
        if len(title) < 3:  # Skip very short titles
            continue

        # Case-insensitive search for title
        pattern = rf'\b{re.escape(title)}\b'
        for match in re.finditer(pattern, content, re.IGNORECASE):
            mentions.append((title, match.start()))

    return mentions


def suggest_moc_links(note_path: Path, moc_mapping: dict[str, str]) -> list[str]:
    """Suggest MOC links based on domain/tags."""
    suggestions = []
    normalized = note_path.with_suffix("").as_posix()

    for prefix, target in sorted(moc_mapping.items(), key=lambda item: -len(item[0])):
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            suggestions.append(target)

    return suggestions


def build_moc_mapping(vault_path: Path) -> dict[str, str]:
    """Build mapping from domains to their MOC files."""
    mapping = {}
    moc_dir = vault_path / "MOC"

    if moc_dir.exists():
        available_mocs = {moc_file.stem for moc_file in moc_dir.glob("*.md")}
        explicit_mapping = {
            "thoughts/ideas": "MOC-ideas",
            "thoughts/learnings": "MOC-learnings",
            "thoughts/reflections": "MOC-reflections",
            "thoughts/projects": "MOC-projects",
            "projects": "MOC-projects",
        }
        for prefix, moc_name in explicit_mapping.items():
            if moc_name in available_mocs:
                mapping[prefix] = moc_name

    return mapping


def build_unique_stem_index(md_files: list[Path], vault_path: Path) -> dict[str, str]:
    """Map note stems to vault note keys only when the stem is unique."""
    candidates: dict[str, set[str]] = defaultdict(set)
    for md_file in md_files:
        candidates[md_file.stem].add(note_key(vault_path, md_file))
    return {
        stem: next(iter(matches))
        for stem, matches in candidates.items()
        if len(matches) == 1
    }


def analyze_and_suggest(vault_path: Path) -> dict:
    """Analyze vault and suggest new links."""
    suggestions: dict[str, list[dict]] = defaultdict(list)

    # Collect all note titles
    md_files = list(vault_path.rglob("*.md"))
    md_files = [
        f for f in md_files
        if f.relative_to(vault_path).parts[:1] != ("skills",)
        if not any(part.startswith('.') for part in f.relative_to(vault_path).parts)
    ]

    unique_stems = build_unique_stem_index(md_files, vault_path)
    moc_mapping = build_moc_mapping(vault_path)

    for md_file in md_files:
        rel_path = md_file.relative_to(vault_path)
        source_key = note_key(vault_path, md_file)
        title = md_file.stem
        content = md_file.read_text(encoding="utf-8", errors="ignore")

        # Find unlinked mentions
        mentions = find_mentions(content, set(unique_stems) - {title})
        for mentioned_title, position in mentions:
            target_key = unique_stems.get(mentioned_title)
            if target_key is None:
                continue
            suggestions[source_key].append({
                "type": "mention",
                "target": target_key,
                "position": position,
                "reason": f"'{mentioned_title}' mentioned but not linked",
            })

        # Suggest MOC links for orphan notes
        existing_links = extract_existing_links(content)
        moc_suggestions = suggest_moc_links(rel_path, moc_mapping)
        for moc in moc_suggestions:
            if moc not in existing_links:
                suggestions[source_key].append({
                    "type": "moc",
                    "target": moc,
                    "reason": f"Should link to [[{moc}]] MOC",
                })

    return dict(suggestions)


def apply_link(file_path: Path, target: str, dry_run: bool = True) -> bool:
    """Add a link to a note's related section."""
    content = file_path.read_text(encoding="utf-8")

    # Check if link already exists
    if f"[[{target}]]" in content:
        return False

    # Find or create "Related" section
    related_pattern = r'^## Related\s*$'
    match = re.search(related_pattern, content, re.MULTILINE)

    if match:
        # Add to existing Related section
        insert_pos = match.end()
        new_content = content[:insert_pos] + f"\n- [[{target}]]" + content[insert_pos:]
    else:
        # Add Related section at end
        new_content = content.rstrip() + f"\n\n## Related\n\n- [[{target}]]\n"

    if dry_run:
        print(f"[DRY RUN] Would add [[{target}]] to {file_path.name}")
        return True

    file_path.write_text(new_content, encoding="utf-8")
    print(f"Added [[{target}]] to {file_path.name}")
    return True


def format_suggestions(suggestions: dict) -> str:
    """Format suggestions as readable report."""
    if not suggestions:
        return "No link suggestions found. Vault is well-connected!"

    lines = [
        "# Link Suggestions",
        "",
        f"Found suggestions for {len(suggestions)} notes:",
        "",
    ]

    for note, items in sorted(suggestions.items()):
        lines.append(f"## [[{note}]]")
        for item in items:
            lines.append(f"- {item['type'].upper()}: Link to [[{item['target']}]]")
            lines.append(f"  - Reason: {item['reason']}")
        lines.append("")

    return "\n".join(lines)


def format_html(suggestions: dict) -> str:
    """Format suggestions as Telegram HTML."""
    if not suggestions:
        return "✅ <b>No link suggestions</b>\n\nVault is well-connected!"

    total = sum(len(v) for v in suggestions.values())

    lines = [
        "🔗 <b>Link Suggestions</b>",
        "",
        f"<b>Found:</b> {total} suggestions for {len(suggestions)} notes",
        "",
    ]

    # Show top 10 suggestions
    count = 0
    for note, items in sorted(suggestions.items(), key=lambda x: -len(x[1])):
        if count >= 10:
            remaining = total - count
            lines.append(f"\n<i>... and {remaining} more suggestions</i>")
            break
        for item in items:
            lines.append(f"• [[{note}]] → [[{item['target']}]]")
            count += 1
            if count >= 10:
                break

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    apply_mode = "--apply" in sys.argv

    # Get vault path
    vault_path = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            vault_path = Path(arg)
            break

    if vault_path is None:
        # Default: script lives in skills/graph-builder/scripts/
        vault_path = Path(__file__).parent.parents[2] / "vault"

    if not vault_path.exists():
        print(f"Error: Vault path not found: {vault_path}", file=sys.stderr)
        sys.exit(1)

    suggestions = analyze_and_suggest(vault_path)

    if apply_mode:
        # Apply all suggestions
        applied = 0
        for note, items in suggestions.items():
            note_path = vault_path / f"{note}.md"
            if note_path.exists():
                for item in items:
                    if apply_link(note_path, item["target"], dry_run):
                        applied += 1

        print(f"\n{'[DRY RUN] Would apply' if dry_run else 'Applied'} {applied} links")
    else:
        # Just report
        if "--html" in sys.argv:
            print(format_html(suggestions))
        elif "--json" in sys.argv:
            import json
            print(json.dumps(suggestions, indent=2))
        else:
            print(format_suggestions(suggestions))


if __name__ == "__main__":
    main()
