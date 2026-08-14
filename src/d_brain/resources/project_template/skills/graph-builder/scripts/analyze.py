#!/usr/bin/env python3
"""
Graph Analyzer for Obsidian Vault.

Analyzes wiki-link structure, writes .graph artifacts, and prints a report.
Run with: uv run analyze.py [vault_path]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_SRC = Path(__file__).parent.parents[2] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from d_brain.manifest import VaultManifest, load_manifest_for_vault  # noqa: E402
from d_brain.services.frontmatter import (  # noqa: E402
    atomic_write_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.vault_lock import vault_write_lock  # noqa: E402

WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+?)(?:\\?\|[^\]]+)?\]\]")
NON_NOTE_PATH_PREFIXES = {"src", "tests", "scripts", "deploy", "docs"}
NON_NOTE_FILE_NAMES = {"README", "README.ru", ".env", "pyproject.toml", "uv.lock"}
NON_NOTE_EXTENSIONS = {
    ".json",
    ".py",
    ".service",
    ".sh",
    ".timer",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
HEALTH_HISTORY_LIMIT = 90
DAILY_STRUCTURE_PENALTY = 4.0
BROKEN_LINK_PENALTY = 0.5
CODE_SPAN_PATTERN = re.compile(r"`[^`\n]*`")
CODE_FENCE_PATTERN = re.compile(r"^\s*(?:```|~~~)")


def _blank_code(content: str) -> str:
    """Blank out fenced blocks and inline code spans.

    Obsidian does not render a wikilink inside code, and the runtime relies
    on that: ``services/plaud.py`` writes a raw payload reference as
    ``` `[[imports/plaud/raw/...json]]` ``` precisely so it shows the path
    without claiming the file is a note. Counting those anyway cost nothing
    while every non-note target was discarded unchecked, but
    ``_non_note_target_exists`` now decides broken-vs-ignored by whether the
    target is on disk -- and payload retention deletes those JSON files on
    schedule. Left in, the 539 deliberately-defused references in
    ``imports/`` would turn into broken links the moment retention ran,
    burying real breakage under noise the code had already opted out of.
    """
    lines: list[str] = []
    in_fence = False
    for line in content.splitlines():
        if CODE_FENCE_PATTERN.match(line):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else CODE_SPAN_PATTERN.sub(" ", line))
    return "\n".join(lines)


def extract_links(content: str) -> list[str]:
    """Extract raw [[wiki-links]] Obsidian would actually render."""
    return WIKILINK_PATTERN.findall(_blank_code(content))


def _note_key(vault_path: Path, note_path: Path) -> str:
    """Stable note id used inside the graph."""
    return note_path.relative_to(vault_path).with_suffix("").as_posix()


def _domain_for(note_path: Path) -> str:
    """Vault-domain bucket for one note's relative path.

    The ``compiled/`` layer holds six domains (projects, people, topics,
    decisions, meetings, concepts) plus an archive folder that mirrors those
    same six domains one level deeper (``compiled/archive/<domain>/``, see
    ``CompiledBriefingService._archive_candidate``). Bucketing all of them
    under a single ``compiled`` domain would collapse six unrelated stats
    rows into one and would hide the archive from
    ``_domain_for``-derived reporting, so both are split by subdirectory
    instead: ``compiled/<domain>`` and ``compiled/archive/<domain>``.
    """
    rel_parts = note_path.parts
    if rel_parts[:1] == ("compiled",):
        if rel_parts[1:2] == ("archive",) and len(rel_parts) > 3:
            return f"compiled/archive/{rel_parts[2]}"
        if len(rel_parts) > 1:
            return f"compiled/{rel_parts[1]}"
    return rel_parts[0] if len(rel_parts) > 1 else "root"


def _normalize_link_target(target: str) -> str:
    """Normalize wikilink target to a vault-style path without .md / anchors."""
    normalized = target.strip().replace("\\", "/")
    normalized = normalized.split("#", 1)[0].split("^", 1)[0].strip()
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    if normalized.startswith("vault/"):
        normalized = normalized[6:]
    return str(PurePosixPath(normalized)) if normalized else ""


def _ignored_link_reason(source_key: str, target: str) -> str | None:
    """Classify unresolved wikilinks that are not real note graph gaps."""
    if not target:
        return None

    target_path = PurePosixPath(target)
    first_part = target_path.parts[0] if target_path.parts else ""

    if target.startswith("imports/plaud/raw/") and target.endswith(".json"):
        return "plaud-raw-payload"
    if first_part.startswith("."):
        return "hidden-path"
    if first_part in NON_NOTE_PATH_PREFIXES:
        return "repo-path"
    if target in NON_NOTE_FILE_NAMES or target_path.suffix in NON_NOTE_EXTENSIONS:
        return "repo-file"
    if source_key.startswith("daily/") and first_part in {"README", "README.ru"}:
        return "repo-file"
    return None


def _non_note_target_exists(vault_path: Path, target: str) -> bool:
    """Whether a non-note wikilink target is a file that is actually there.

    ``_ignored_link_reason`` classifies by the shape of the path alone, so a
    live ``[[src/d_brain/services/qmd.py]]`` and a ``[[tests/deleted.py]]``
    whose target was removed months ago were indistinguishable -- both were
    dropped before the broken-link report. That is how 92 dead links sat in
    ``daily/`` while every health run still scored the vault 99.6 and
    reported ``broken_links: 0``. Classification still decides *that* a
    target is not a note; this decides whether it is still reachable.

    Both roots are probed, the way ``CompiledBriefingService.
    _lint_source_base_dirs`` probes them: the namespaces overlap, since
    ``.claude/`` exists in the vault (rules, docs) and in the project root
    (the shared skills tree). Every target is also retried with ``.md``
    appended, because ``_normalize_link_target`` strips that extension on
    the way in. Appending rather than replacing the suffix is what makes
    ``[[README.ru]]`` resolve: ``PurePosixPath`` reads its ``.ru`` as a
    suffix, so ``with_suffix(".md")`` probed for ``README.md`` -- a
    different file that happens to exist -- while the real target,
    ``README.ru.md``, was never tried at all. ``resolve()`` plus the
    containment check keeps a ``../`` target from reporting a file outside
    either root as present.
    """
    if not target:
        return False
    for base in (vault_path, vault_path.parent):
        base = base.resolve()
        for name in (target, f"{target}.md"):
            candidate = (base / name).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                continue
            if candidate.exists():
                return True
    return False


def _build_indexes(
    vault_path: Path,
    md_files: list[Path],
) -> tuple[dict[str, dict[str, Any]], dict[str, str | None]]:
    """Build note metadata and unique stem lookup."""
    notes: dict[str, dict[str, Any]] = {}
    stem_candidates: dict[str, set[str]] = defaultdict(set)

    for md_file in md_files:
        rel_path = md_file.relative_to(vault_path)
        note_key = _note_key(vault_path, md_file)
        note_stem = md_file.stem
        notes[note_key] = {
            "title": note_stem,
            "path": rel_path.as_posix(),
            "domain": _domain_for(rel_path),
            "size": md_file.stat().st_size,
        }
        stem_candidates[note_stem].add(note_key)

    stem_index: dict[str, str | None] = {}
    for stem, candidates in stem_candidates.items():
        if len(candidates) == 1:
            stem_index[stem] = next(iter(candidates))
        else:
            stem_index[stem] = None

    return notes, stem_index


def _has_description(content: str) -> bool:
    """Best-effort frontmatter description detector."""
    if not content.startswith("---\n"):
        return False
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if match is None:
        return False
    return re.search(r"^description:\s*.+$", match.group(1), re.MULTILINE) is not None


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split one leading YAML frontmatter block from the body."""
    match = re.match(r"^---\n(.*?)\n---\n*", content or "", re.DOTALL)
    if match is None:
        return "", str(content or "")
    return match.group(1), str(content or "")[match.end() :]


def _daily_structure_issue(note_path: Path, content: str) -> str | None:
    """Validate canonical daily shape: frontmatter first, then matching H1."""
    expected_heading = f"# {note_path.stem}"
    if not str(content or "").startswith("---\n"):
        return "frontmatter-not-first"

    _, body = _split_frontmatter(content)
    first_nonempty = next(
        (line.strip() for line in body.splitlines() if line.strip()), ""
    )
    if not first_nonempty:
        return "missing-h1"
    if first_nonempty != expected_heading:
        return "unexpected-h1" if first_nonempty.startswith("# ") else "missing-h1"
    return None


def _resolve_link_target(
    raw_target: str,
    *,
    notes: dict[str, dict[str, Any]],
    stem_index: dict[str, str | None],
) -> str | None:
    """Resolve a wikilink target to one note key."""
    normalized = _normalize_link_target(raw_target)
    if not normalized:
        return None
    if normalized in notes:
        return normalized
    if "/" not in normalized:
        return stem_index.get(normalized)
    return None


def _display_link(note_key: str, notes: dict[str, dict[str, Any]]) -> str:
    """Human-friendly wikilink for reports."""
    info = notes[note_key]
    title = str(info["title"])
    if note_key == title:
        return f"[[{note_key}]]"
    return f"[[{note_key}|{title}]]"


def analyze_vault(vault_path: Path) -> dict[str, Any]:
    """Analyze vault link structure."""
    md_files = [
        file_path
        for file_path in vault_path.rglob("*.md")
        if file_path.relative_to(vault_path).parts[:1] != ("skills",)
        if not any(
            part.startswith(".")
            for part in file_path.relative_to(vault_path).parts
        )
    ]

    notes, stem_index = _build_indexes(vault_path, md_files)
    links_from: dict[str, set[str]] = defaultdict(set)
    links_to: dict[str, set[str]] = defaultdict(set)
    broken_links: list[dict[str, str]] = []
    ignored_links: list[dict[str, str]] = []
    total_wikilinks = 0

    for md_file in md_files:
        source_key = _note_key(vault_path, md_file)
        content = md_file.read_text(encoding="utf-8", errors="ignore")
        notes[source_key]["has_description"] = _has_description(content)
        for raw_link in extract_links(content):
            total_wikilinks += 1
            target_key = _resolve_link_target(
                raw_link,
                notes=notes,
                stem_index=stem_index,
            )
            if target_key is None:
                normalized_target = _normalize_link_target(raw_link) or raw_link.strip()
                ignored_reason = _ignored_link_reason(source_key, normalized_target)
                if ignored_reason is not None and _non_note_target_exists(
                    vault_path,
                    normalized_target,
                ):
                    ignored_links.append(
                        {
                            "source": source_key,
                            "target": normalized_target,
                            "reason": ignored_reason,
                        }
                    )
                    continue
                broken_links.append(
                    {
                        "source": source_key,
                        "target": normalized_target,
                    }
                )
                continue
            links_from[source_key].add(target_key)
            links_to[target_key].add(source_key)

    orphans: list[str] = []
    weakly_connected: list[str] = []
    archived_isolated: list[str] = []
    malformed_daily: list[dict[str, str]] = []
    description_candidates = 0
    described_notes = 0
    for note_key, info in notes.items():
        incoming = len(links_to.get(note_key, set()))
        outgoing = len(links_from.get(note_key, set()))
        info["incoming"] = incoming
        info["outgoing"] = outgoing
        info["total_links"] = incoming + outgoing
        domain = str(info["domain"])
        is_archived = domain.startswith("compiled/archive")
        if is_archived:
            # No incoming links is the archival trigger itself (ТЗ 6.4: a
            # page moves to compiled/archive/ once it has no incoming
            # links), not a graph defect -- counting it as an orphan would
            # make every correctly archived page shout forever in the
            # report. Tracked separately so the fact still shows up.
            if incoming == 0 and outgoing == 0:
                archived_isolated.append(note_key)
        elif incoming == 0 and outgoing == 0 and domain not in {"MOC", "root"}:
            orphans.append(note_key)
        elif info["total_links"] < 2 and domain not in {"MOC", "root", "daily"}:
            weakly_connected.append(note_key)
        if info["domain"] == "daily":
            note_path = vault_path / str(info["path"])
            daily_issue = _daily_structure_issue(
                note_path,
                note_path.read_text(encoding="utf-8", errors="ignore"),
            )
            if daily_issue is not None:
                info["daily_structure_issue"] = daily_issue
                malformed_daily.append(
                    {
                        "path": note_key,
                        "issue": daily_issue,
                    }
                )
        else:
            description_candidates += 1
            if info["has_description"]:
                described_notes += 1

    domain_stats: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"count": 0, "links": 0}
    )
    for info in notes.values():
        domain = str(info["domain"])
        domain_stats[domain]["count"] += 1
        domain_stats[domain]["links"] += int(info["total_links"])

    for _domain, data in domain_stats.items():
        count = int(data["count"])
        data["avg_links"] = (float(data["links"]) / count) if count else 0.0

    most_connected = sorted(
        notes.items(),
        key=lambda item: int(item[1]["total_links"]),
        reverse=True,
    )[:10]

    avg_links = (
        sum(int(info["total_links"]) for info in notes.values()) / len(notes)
        if notes
        else 0.0
    )
    orphan_ratio = (len(orphans) / len(notes)) if notes else 0.0
    description_ratio = (
        described_notes / description_candidates if description_candidates else 1.0
    )
    daily_structure_penalty = min(
        20.0,
        len(malformed_daily) * DAILY_STRUCTURE_PENALTY,
    )
    # Counted per link, not as a share of all wikilinks. The share made a
    # broken link cheaper the more links a vault had: against 17568
    # wikilinks -- a number MOC hubs alone inflate, one summary page carries
    # 811 -- 52 dead links moved the score by 0.09, so a defect the owner
    # has to repair by hand read as noise. Same per-item shape as
    # daily_structure_penalty, and capped for the same reason: one category
    # should not be able to zero the score on its own.
    broken_link_penalty = min(20.0, len(broken_links) * BROKEN_LINK_PENALTY)
    health_score = max(
        0.0,
        100.0
        - (orphan_ratio * 30.0)
        - broken_link_penalty
        - max(0.0, (3.0 - avg_links) * 15.0)
        - ((1.0 - description_ratio) * 10.0)
        - daily_structure_penalty,
    )

    return {
        "total_notes": len(notes),
        "total_links": sum(len(targets) for targets in links_from.values()),
        "total_wikilinks": total_wikilinks,
        "broken_links": broken_links,
        "broken_link_count": len(broken_links),
        "ignored_links": ignored_links,
        "ignored_link_count": len(ignored_links),
        "ignored_link_reasons": dict(
            sorted(Counter(item["reason"] for item in ignored_links).items())
        ),
        "orphans": sorted(orphans),
        "orphan_count": len(orphans),
        "weakly_connected": sorted(weakly_connected),
        "weakly_connected_count": len(weakly_connected),
        "archived_isolated": sorted(archived_isolated),
        "archived_isolated_count": len(archived_isolated),
        "malformed_daily_notes": malformed_daily,
        "malformed_daily_count": len(malformed_daily),
        "daily_structure_penalty": round(daily_structure_penalty, 1),
        "avg_links_per_note": round(avg_links, 2),
        "description_count": described_notes,
        "description_candidate_count": description_candidates,
        "description_ratio": round(description_ratio, 3),
        "health_score": round(health_score, 1),
        "domain_stats": dict(domain_stats),
        "most_connected": [
            {"note": note_key, "links": int(info["total_links"])}
            for note_key, info in most_connected
        ],
        "notes": notes,
        "links_from": {key: sorted(value) for key, value in links_from.items()},
        "links_to": {key: sorted(value) for key, value in links_to.items()},
    }


def format_report(stats: dict[str, Any]) -> str:
    """Format analysis as readable report."""
    lines = [
        "# Vault Graph Analysis",
        "",
        "## Overview",
        "",
        f"- **Total notes:** {stats['total_notes']}",
        f"- **Resolved links:** {stats['total_links']}",
        f"- **Raw wikilinks:** {stats['total_wikilinks']}",
        f"- **Health score:** {stats['health_score']:.1f}/100",
        f"- **Avg links/note:** {stats['avg_links_per_note']:.2f}",
        f"- **Broken links:** {stats['broken_link_count']}",
        f"- **Ignored non-note refs:** {stats['ignored_link_count']}",
        f"- **Orphan notes:** {stats['orphan_count']}",
        f"- **Archived notes with no links (expected):** {stats['archived_isolated_count']}",
        f"- **Malformed daily notes:** {stats['malformed_daily_count']}",
        (
            f"- **Description coverage:** {stats['description_count']}/"
            f"{stats['description_candidate_count']} "
            f"({stats['description_ratio']:.0%})"
        ),
        "",
    ]

    if stats["most_connected"]:
        lines.append("## Most Connected Notes")
        lines.append("")
        for item in stats["most_connected"][:5]:
            note_ref = _display_link(item["note"], stats["notes"])
            lines.append(f"- {note_ref} ({item['links']} links)")
        lines.append("")

    lines.append("## Domain Statistics")
    lines.append("")
    lines.append("| Domain | Notes | Avg Links |")
    lines.append("|--------|-------|-----------|")
    for domain, data in sorted(stats["domain_stats"].items()):
        lines.append(
            f"| {domain}/ | {int(data['count'])} | {float(data['avg_links']):.1f} |"
        )
    lines.append("")

    if stats["ignored_link_reasons"]:
        lines.append("## Ignored Non-note References")
        lines.append("")
        for reason, count in sorted(stats["ignored_link_reasons"].items()):
            lines.append(f"- `{reason}`: {count}")
        lines.append("")

    if stats["broken_links"]:
        lines.append("## Broken Links")
        lines.append("")
        for item in stats["broken_links"][:20]:
            lines.append(f"- `{item['source']}` -> `[[{item['target']}]]`")
        if len(stats["broken_links"]) > 20:
            lines.append(f"- ... and {len(stats['broken_links']) - 20} more")
        lines.append("")

    if stats["malformed_daily_notes"]:
        lines.append("## Malformed Daily Notes")
        lines.append("")
        for item in stats["malformed_daily_notes"][:20]:
            lines.append(f"- `{item['path']}` ({item['issue']})")
        if len(stats["malformed_daily_notes"]) > 20:
            lines.append(
                f"- ... and {len(stats['malformed_daily_notes']) - 20} more"
            )
        lines.append("")

    if stats["orphans"]:
        lines.append("## Orphan Notes (need links)")
        lines.append("")
        for note_key in stats["orphans"][:20]:
            lines.append(f"- {_display_link(note_key, stats['notes'])}")
        if len(stats["orphans"]) > 20:
            lines.append(f"- ... and {len(stats['orphans']) - 20} more")
        lines.append("")

    if stats["weakly_connected"]:
        lines.append("## Weakly Connected Notes")
        lines.append("")
        for note_key in stats["weakly_connected"][:20]:
            lines.append(f"- {_display_link(note_key, stats['notes'])}")
        if len(stats["weakly_connected"]) > 20:
            lines.append(
                f"- ... and {len(stats['weakly_connected']) - 20} more"
            )
        lines.append("")

    if stats["archived_isolated"]:
        lines.append("## Archived Notes With No Links (Expected, Not A Defect)")
        lines.append("")
        lines.append(
            "A `compiled/archive/` page with no incoming links is the "
            "archival trigger, not a graph problem -- listed here for "
            "visibility only."
        )
        lines.append("")
        for note_key in stats["archived_isolated"][:20]:
            lines.append(f"- {_display_link(note_key, stats['notes'])}")
        if len(stats["archived_isolated"]) > 20:
            lines.append(
                f"- ... and {len(stats['archived_isolated']) - 20} more"
            )
        lines.append("")

    return "\n".join(lines)


def format_html(stats: dict[str, Any]) -> str:
    """Format analysis as Telegram HTML."""
    orphan_count = stats["orphan_count"]
    orphan_emoji = "⚠️" if orphan_count > 10 else "✅"

    lines = [
        "📊 <b>Vault Graph Analysis</b>",
        "",
        f"<b>📝 Total notes:</b> {stats['total_notes']}",
        f"<b>🔗 Resolved links:</b> {stats['total_links']}",
        f"<b>💯 Health:</b> {stats['health_score']:.1f}/100",
        f"<b>🧩 Broken links:</b> {stats['broken_link_count']}",
        f"<b>🗂 Ignored refs:</b> {stats['ignored_link_count']}",
        f"<b>{orphan_emoji} Orphan notes:</b> {orphan_count}",
        f"<b>🧾 Malformed daily:</b> {stats['malformed_daily_count']}",
        "",
    ]

    if stats["most_connected"]:
        lines.append("<b>🏆 Most connected:</b>")
        for item in stats["most_connected"][:3]:
            title = stats["notes"][item["note"]]["title"]
            lines.append(f"• {title} ({item['links']})")
        lines.append("")

    weakest = min(
        stats["domain_stats"].items(),
        key=lambda item: float(item[1].get("avg_links", 0)),
    )
    lines.append(
        "<b>⚡ Weakest domain:</b> "
        f"{weakest[0]}/ (avg {float(weakest[1].get('avg_links', 0)):.1f} links)"
    )

    if stats["broken_links"]:
        lines.append("")
        lines.append("<b>🛠 Broken link sample:</b>")
        for item in stats["broken_links"][:5]:
            lines.append(f"• {item['source']} -> [[{item['target']}]]")

    return "\n".join(lines)


def _append_history(graph_dir: Path, stats: dict[str, Any]) -> None:
    """Append one health data point, keeping a bounded history."""
    history_path = graph_dir / "health-history.json"
    entry = {
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "health_score": stats["health_score"],
        "total_notes": stats["total_notes"],
        "total_links": stats["total_links"],
        "avg_links_per_note": stats["avg_links_per_note"],
        "broken_links": stats["broken_link_count"],
        "orphans": stats["orphan_count"],
        "weakly_connected": stats["weakly_connected_count"],
        "malformed_daily": stats["malformed_daily_count"],
        "description_ratio": stats["description_ratio"],
    }
    history: list[dict[str, Any]] = []
    if history_path.exists():
        try:
            raw = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                history = raw
        except json.JSONDecodeError:
            history = []
    history.append(entry)
    history = history[-HEALTH_HISTORY_LIMIT:]
    atomic_write_bytes(
        history_path,
        (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _manifest(vault_path: Path) -> VaultManifest:
    return load_manifest_for_vault(vault_path)


def _technical_report(stats: dict[str, Any]) -> str:
    """Render the generated report with the technical profile up front."""
    today = datetime.now().astimezone().date().isoformat()
    return (
        "---\n"
        "type: technical\n"
        f"last_accessed: {today}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        + format_report(stats).rstrip()
        + "\n"
    )


def write_artifacts(vault_path: Path, stats: dict[str, Any]) -> None:
    """Persist graph outputs into vault/.graph."""
    graph_dir = vault_path / ".graph"
    manifest = _manifest(vault_path)
    with vault_write_lock(vault_path) as lock:
        graph_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            graph_dir / "vault-graph.json",
            (json.dumps(stats, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        write_validated_vault_markdown(
            vault_path,
            graph_dir / "report.md",
            _technical_report(stats).encode("utf-8"),
            manifest=manifest,
            existing_lock=lock,
        )
        _append_history(graph_dir, stats)


def main() -> None:
    if len(sys.argv) > 1:
        vault_path = Path(sys.argv[1])
    else:
        vault_path = Path(__file__).parent.parents[2] / "vault"
    if not vault_path.exists():
        print(f"Error: Vault path not found: {vault_path}", file=sys.stderr)
        raise SystemExit(1)

    stats = analyze_vault(vault_path)
    write_artifacts(vault_path, stats)

    if "--html" in sys.argv:
        print(format_html(stats))
    elif "--json" in sys.argv:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(format_report(stats))


if __name__ == "__main__":
    main()
