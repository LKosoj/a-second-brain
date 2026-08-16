#!/usr/bin/env python3
"""
Add description field to frontmatter of vault files.

Strategy by file type:
- CRM files: "{title} — {type}, {industry}, {status}, {deal_status}"
- Thoughts: first non-empty paragraph after frontmatter, truncated to 150 chars
- MOC: "Map of Content: {topic}, N entries"
- Goals: from first paragraph
- Summaries: "Weekly summary for {week}"
- Other: first paragraph, truncated to 150 chars

Skips:
- daily/ files (don't need descriptions per convention)
- Files that already have description field

Usage:
  uv run skills/vault-health/scripts/add_descriptions.py
  uv run skills/vault-health/scripts/add_descriptions.py --apply
"""

import hashlib
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
VAULT_PATH = PROJECT_ROOT / "vault"
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from d_brain.manifest import load_manifest_for_vault  # noqa: E402
from d_brain.services.frontmatter import (  # noqa: E402
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    patch_validated_vault_frontmatter,
    route_profile,
)
from d_brain.services.vault_lock import vault_write_lock  # noqa: E402

IGNORE_DIRS = {".obsidian", "attachments", ".git", ".graph", ".claude", ".trash", "skills"}
IGNORE_PATTERNS = {"backup", ".backup", "archive"}
SEMANTIC_DESCRIPTION_PROFILES = frozenset(
    {
        "derived",
        "thought-card",
        "reflection",
        "goal",
        "index",
        "flat-context",
        "epistemic",
        "home",
    }
)
def parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter as simple key-value pairs."""
    match = re.match(r"^\ufeff?---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split("\n"):
        stripped = line.strip()
        is_scalar = ":" in line
        is_list_or_comment = stripped.startswith("-") or stripped.startswith("#")
        if is_scalar and not is_list_or_comment:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if (
                key
                and value
                and not value.startswith("[")
                and not value.startswith("{")
            ):
                fm[key] = value
    return fm


def get_body_after_frontmatter(content: str) -> str:
    """Get content after frontmatter."""
    match = re.match(r"^\ufeff?---\n.*?\n---\n?", content, re.DOTALL)
    if match:
        return content[match.end():]
    return content


def extract_first_paragraph(body: str) -> str:
    """Extract first non-empty, non-heading paragraph from body."""
    lines = body.split("\n")
    paragraph_lines = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()

        # Skip empty lines before paragraph starts
        if not stripped and not in_paragraph:
            continue

        # Skip headings, callouts, code blocks, links-only lines
        if stripped.startswith(("#", ">", "```", "---", "| ", "- [[", "![")):
            if in_paragraph:
                break
            continue

        if stripped:
            in_paragraph = True
            paragraph_lines.append(stripped)
        elif in_paragraph:
            break

    text = " ".join(paragraph_lines)
    # Remove wikilinks: [[path|text]] → text, [[path]] → path
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    # Remove markdown formatting
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def truncate(text: str, max_len: int = 150) -> str:
    """Truncate text to max_len on word boundary."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Find last space
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,;: ") + "..."


def generate_description(rel_path: str, content: str, fm: dict) -> str | None:
    """Generate a description for a file based on its type and content."""
    default_title = Path(rel_path).stem.replace("-", " ").title()

    # Business files
    if rel_path.startswith("business/"):
        title_match = re.search(r"^# (.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else default_title

        parts = []
        if fm.get("type") and fm["type"] != "note":
            parts.append(fm["type"])
        if fm.get("industry"):
            parts.append(fm["industry"])
        if fm.get("status"):
            parts.append(fm["status"])
        if fm.get("deal_status"):
            parts.append(fm["deal_status"])
        if fm.get("region"):
            parts.append(fm["region"])

        if parts:
            return truncate(f"{title} — {', '.join(parts)}")

        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return truncate(f"{title} — Business context")

    # Projects files
    if rel_path.startswith("projects/"):
        title_match = re.search(r"^# (.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else default_title

        parts = []
        if fm.get("type") and fm["type"] != "project":
            parts.append(fm["type"])
        if fm.get("industry"):
            parts.append(fm["industry"])
        if fm.get("status"):
            parts.append(fm["status"])
        if parts:
            return truncate(f"{title} — {', '.join(parts)}")

        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return truncate(f"{title} — Projects")

    # MOC files
    if rel_path.startswith("MOC/"):
        topic = Path(rel_path).stem.removeprefix("MOC-").replace("-", " ").title()
        link_count = content.count("[[")
        return f"Map of Content: {topic}, {link_count} entries"

    # Thoughts
    if rel_path.startswith("thoughts/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        # Fallback: use filename
        stem = Path(rel_path).stem
        # Remove date prefix if present
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        return stem.replace("-", " ").capitalize()

    # Goals
    if rel_path.startswith("goals/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return f"Goal: {Path(rel_path).stem.replace('-', ' ')}"

    # Family
    if rel_path.startswith("family/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return f"Family: {Path(rel_path).stem.replace('-', ' ').title()}"

    # Private
    if rel_path.startswith("private/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return f"Private: {Path(rel_path).stem.replace('-', ' ').title()}"

    # Hobbies
    if rel_path.startswith("hobbies/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        stem = Path(rel_path).stem
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        return f"Hobby: {stem.replace('-', ' ').title()}"

    # Finances
    if rel_path.startswith("finances/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        stem = Path(rel_path).stem
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        return f"Finances: {stem.replace('-', ' ').title()}"

    # Summaries
    if rel_path.startswith("summaries/"):
        stem = Path(rel_path).stem
        week_match = re.search(r"(\d{4}-W\d{2})", stem)
        if week_match:
            return f"Weekly summary for {week_match.group(1)}"
        return f"Summary: {stem.replace('-', ' ')}"

    # Contacts
    if rel_path.startswith("contacts/"):
        body = get_body_after_frontmatter(content)
        para = extract_first_paragraph(body)
        if para:
            return truncate(para)
        return f"Contacts: {Path(rel_path).stem.replace('-', ' ')}"

    # _index files
    if Path(rel_path).stem == "_index":
        parent = Path(rel_path).parent.name
        return f"Index and entry point for {parent}"

    # MEMORY.md
    if Path(rel_path).name == "MEMORY.md":
        return "Long-term memory and key decisions"

    # Default: first paragraph
    body = get_body_after_frontmatter(content)
    para = extract_first_paragraph(body)
    if para:
        return truncate(para)

    return None


def add_description_to_frontmatter(content: str, description: str) -> str:
    """Losslessly add a description without serializing unrelated YAML."""
    return patch_frontmatter_bytes(
        content.encode("utf-8"), {"description": description}
    ).decode("utf-8")


def collect_candidate_files() -> list[Path]:
    """Vault notes this script may rewrite the frontmatter of.

    Hidden folders are skipped wholesale, the way
    ``graph-builder/scripts/analyze.py`` walks the same vault. Listing them
    one by one left ``.session`` in, whose
    ``compile-enrich/snapshots/<hash>/blobs/`` tree holds the exact bytes
    the compile layer diffs the next pass against -- patching a description
    into those would make a page look changed when it was not. Matching is
    on the vault-relative path so a directory above the vault cannot
    trigger it.
    """
    candidates = []
    for file_path in VAULT_PATH.rglob("*.md"):
        rel_path = file_path.relative_to(VAULT_PATH)
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        if any(part in IGNORE_DIRS for part in rel_path.parts):
            continue
        if any(pattern in rel_path.as_posix() for pattern in IGNORE_PATTERNS):
            continue
        candidates.append(file_path)
    return candidates


def main():
    apply = "--apply" in sys.argv
    manifest = load_manifest_for_vault(VAULT_PATH)

    # Get all non-daily files
    all_files = collect_candidate_files()

    print(f"Total markdown files: {len(all_files)}")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print()

    added = 0
    skipped_daily = 0
    already_has = 0
    profile_skipped = 0
    no_description = 0
    errors = 0
    conflicts = 0

    for file_path in sorted(all_files):
        rel_path = str(file_path.relative_to(VAULT_PATH))

        # Skip daily files
        if rel_path.startswith("daily/"):
            skipped_daily += 1
            continue

        try:
            source = file_path.read_bytes()
            source_fingerprint = hashlib.sha256(source).digest()
            content = source.decode("utf-8")
            document = parse_frontmatter_bytes(source)
            profile = route_profile(rel_path, document.fields, manifest)
        except Exception:
            errors += 1
            continue

        if (
            profile.name not in SEMANTIC_DESCRIPTION_PROFILES
            or "description" not in profile.required_fields
        ):
            profile_skipped += 1
            continue

        # Check if already has description
        fm = parse_frontmatter(content)
        if fm.get("description"):
            already_has += 1
            continue

        # Also check raw frontmatter for description: field
        fm_match = re.match(r"^\ufeff?---\n(.*?)\n---", content, re.DOTALL)
        if fm_match and "description:" in fm_match.group(1):
            already_has += 1
            continue

        description = generate_description(rel_path, content, fm)
        if not description:
            no_description += 1
            if "--verbose" in sys.argv:
                print(f"  NO DESC: {rel_path}")
            continue

        preview = description[:80]
        suffix = "..." if len(description) > 80 else ""
        print(f'  {rel_path}: "{preview}{suffix}"')

        if apply:
            with vault_write_lock(VAULT_PATH) as lock:
                try:
                    current = file_path.read_bytes()
                except (FileNotFoundError, OSError):
                    conflicts += 1
                    print(f"  SKIP CONCURRENT: {rel_path}")
                    continue
                if hashlib.sha256(current).digest() != source_fingerprint:
                    conflicts += 1
                    print(f"  SKIP CONCURRENT: {rel_path}")
                    continue
                current_document = parse_frontmatter_bytes(current)
                if "description" in current_document.fields:
                    conflicts += 1
                    print(f"  SKIP CONCURRENT: {rel_path}")
                    continue
                patch_validated_vault_frontmatter(
                    VAULT_PATH,
                    file_path,
                    {"description": description},
                    manifest=manifest,
                    existing_lock=lock,
                )
        added += 1

    non_daily = len(all_files) - skipped_daily
    coverage = (already_has + added) / max(non_daily, 1) * 100

    print(f"\n{'='*50}")
    print(f"Non-daily files: {non_daily}")
    print(f"Already had description: {already_has}")
    print(f"Profile skipped: {profile_skipped}")
    print(f"Descriptions added: {added}")
    print(f"Could not generate: {no_description}")
    print(f"Concurrent conflicts skipped: {conflicts}")
    print(f"Errors: {errors}")
    print(f"Projected coverage: {coverage:.1f}%")
    if apply:
        print(f"Files modified: {added}")


if __name__ == "__main__":
    main()
