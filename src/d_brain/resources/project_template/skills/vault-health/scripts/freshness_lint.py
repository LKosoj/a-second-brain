#!/usr/bin/env python3
"""
Freshness lint for compiled briefings.

Flags "changeable facts" (currency amounts, percentages, thousand/million
counts, item counters, version numbers) on compiled pages that carry no
nearby date and no link to a live source. Compiled pages live under
``vault/compiled/<domain>/*.md`` -- see
``CompiledBriefingService.compiled_root`` in
``d_brain/services/compiled_briefings.py``, the source of truth for that
layout. Any other note (thoughts/, daily/, business/, ...) is out of scope.

This is a report, not a gate: ``main`` always exits 0.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
DEFAULT_VAULT_PATH = PROJECT_ROOT / "vault"

# A heading line starts a "sources" section (skipped until the next heading
# of any level) when its text matches this -- covers both "## Sources" and
# "## Sources That Shaped This Page" from compiled_briefings.py, plus the
# Russian "## Источники". Word boundaries matter: an unbounded "source"
# would also fire on "## Resources Needed" or "## Outsource Costs", which
# have nothing to do with the sources table.
_SOURCES_HEADING_RE = re.compile(r"\bисточник\w*|\bsources?\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

_RU_MONTHS = (
    r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|"
    r"октябр|ноябр|декабр"
)
_EN_MONTHS = (
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december"
)
# Anything that pins a fact to a point in time: an explicit date, an
# "as of"/"по состоянию на" phrase, or a month name next to a year.
_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{2}\.\d{2}\.\d{4}"
    r"|по состоянию на"
    r"|as of\b"
    rf"|\b(?:{_RU_MONTHS})\w*\s+\d{{4}}\b"
    rf"|\b(?:{_EN_MONTHS})\s+\d{{4}}\b",
    re.IGNORECASE,
)
# A link to a live source: a vault wikilink or a bare URL.
_SOURCE_RE = re.compile(r"\[\[[^\]]+\]\]|https?://\S+")

# Same skip-list as fix_links.py/add_descriptions.py (see their IGNORE_DIRS)
# plus a general "any hidden path segment" rule, so scratch/service content
# living inside compiled/ (``.trash``, ``.obsidian``, a stray ``.session``
# copy, ...) is never read as a real compiled page.
_IGNORE_DIRS = {
    ".obsidian",
    "attachments",
    ".git",
    ".graph",
    ".claude",
    ".trash",
    "skills",
}

# Each entry is (reason, pattern). Only number shapes that name a unit,
# currency, or version are treated as "changeable facts" -- a bare year
# (2026), an ordered-list marker (1. ...), or a HH:MM time never matches any
# of these, so they need no separate exclusion.
_FACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "currency amount",
        re.compile(
            r"[$€₽£]\s?\d[\d\s.,]*\d|\d[\d\s.,]*\d\s?(?:USD|EUR|RUB|руб\.?)\b",
            re.IGNORECASE,
        ),
    ),
    ("percentage", re.compile(r"\d+(?:[.,]\d+)?\s?%")),
    (
        "scale suffix (тыс/млн/k/M)",
        re.compile(r"\d+(?:[.,]\d+)?\s?(?:тыс\.?|млн\.?|k|M)\b"),
    ),
    (
        "counter",
        re.compile(
            r"\d+\s+(?:клиент\w*|сотрудник\w*|задач\w*|партнёр\w*|пользовател\w*)",
            re.IGNORECASE,
        ),
    ),
    ("version number", re.compile(r"\bv\d+\.\d+(?:\.\d+)?\b")),
    (
        "balance or price",
        re.compile(r"(?:баланс|цена)\w*[^\d\n]{0,20}\d[\d\s.,]*", re.IGNORECASE),
    ),
]


@dataclass(frozen=True)
class Finding:
    """One changeable fact with no nearby date or source link."""

    path: str
    line: int
    text: str
    reason: str


def _match_fact(line: str) -> str | None:
    for reason, pattern in _FACT_PATTERNS:
        if pattern.search(line):
            return reason
    return None


def _iter_findings(lines: list[str], rel_path: str) -> list[Finding]:
    findings: list[Finding] = []
    in_frontmatter = bool(lines) and lines[0].strip() == "---"
    in_code_block = False
    in_sources_section = False

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\n")

        if in_frontmatter:
            if index > 0 and line.strip() == "---":
                in_frontmatter = False
            continue

        if _CODE_FENCE_RE.match(line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match is not None:
            in_sources_section = bool(
                _SOURCES_HEADING_RE.search(heading_match.group(2))
            )
            continue
        if in_sources_section:
            continue

        reason = _match_fact(line)
        if reason is None:
            continue

        window = lines[max(0, index - 2) : index + 1]
        if any(_DATE_RE.search(w) or _SOURCE_RE.search(w) for w in window):
            continue

        findings.append(
            Finding(path=rel_path, line=index + 1, text=line.strip(), reason=reason)
        )

    return findings


def _is_ignored(rel_path: Path) -> bool:
    return any(part.startswith(".") or part in _IGNORE_DIRS for part in rel_path.parts)


def _lint_file(page_path: Path, rel_path: str) -> list[Finding]:
    try:
        text = page_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _iter_findings(text.splitlines(), rel_path)


def lint_vault(vault_path: Path) -> list[Finding]:
    """Lint every compiled page under ``vault_path/compiled/`` for freshness.

    Returns an empty list if the vault has no ``compiled/`` directory yet.
    Callers (e.g. a weekly report) can import this directly instead of
    shelling out to the CLI below.
    """
    compiled_root = Path(vault_path) / "compiled"
    if not compiled_root.is_dir():
        return []

    findings: list[Finding] = []
    for page_path in sorted(compiled_root.rglob("*.md")):
        rel_path = page_path.relative_to(vault_path)
        if _is_ignored(rel_path):
            continue
        findings.extend(_lint_file(page_path, rel_path.as_posix()))
    return findings


def _parse_args(args: list[str]) -> tuple[Path, bool, int | None, str | None]:
    """Parse CLI args, returning an error message instead of raising.

    ``main`` always exits 0, even on a bad ``--limit`` value -- see its
    docstring -- so parsing failures are reported, not raised.
    """
    vault_path = DEFAULT_VAULT_PATH
    as_json = False
    limit: int | None = None
    positional_seen = False

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--json":
            as_json = True
        elif arg == "--limit":
            index += 1
            if index >= len(args):
                return vault_path, as_json, limit, "--limit requires a number"
            try:
                limit = int(args[index])
            except ValueError:
                return (
                    vault_path,
                    as_json,
                    limit,
                    f"--limit expects an integer, got {args[index]!r}",
                )
        elif not positional_seen and not arg.startswith("--"):
            vault_path = Path(arg)
            positional_seen = True
        index += 1

    return vault_path, as_json, limit, None


def main() -> None:
    vault_path, as_json, limit, error = _parse_args(sys.argv[1:])
    if error is not None:
        print(f"Error: {error}", file=sys.stderr)
        return

    if not vault_path.exists():
        print(f"Error: vault path not found: {vault_path}", file=sys.stderr)
        return

    findings = lint_vault(vault_path)
    total = len(findings)
    shown = findings if limit is None else findings[:limit]
    hidden = total - len(shown)

    if as_json:
        payload = [asdict(finding) for finding in shown]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if total == 0:
        print("No freshness issues found.")
        return

    print(f"{total} freshness issue(s):")
    for finding in shown:
        print(f"{finding.path}:{finding.line}: {finding.text}")
    if hidden > 0:
        print(f"... ещё {hidden} находок скрыто (--limit)")


if __name__ == "__main__":
    main()
