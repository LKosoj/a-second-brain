"""Deterministic audit helpers for MEMORY.md hygiene."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """One extracted MEMORY.md statement."""

    line: int
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class MemoryTheme:
    """One duplicate-prone theme to watch in MEMORY.md."""

    key: str
    title: str
    keywords: tuple[str, ...]
    recommendation: str


@dataclass(frozen=True, slots=True)
class MemoryFinding:
    """Structured audit finding for one overlapping theme."""

    theme: MemoryTheme
    items: tuple[MemoryItem, ...]


@dataclass(frozen=True, slots=True)
class MemoryAuditReport:
    """Rendered duplicate audit for MEMORY.md."""

    findings: tuple[MemoryFinding, ...]

    @property
    def has_issues(self) -> bool:
        return bool(self.findings)

    def to_markdown(self) -> str:
        """Render findings as a compact markdown report for reflect phase."""
        lines = ["# MEMORY Audit", ""]
        if not self.findings:
            lines.extend(
                [
                    "- Status: no duplicate-prone clusters detected.",
                    "",
                    "## Guardrails",
                    "- Keep one canonical policy per theme.",
                    "- `Ключевые решения` = durable policy and architecture.",
                    "- `Правила для запоминания` = short operational heuristics only.",
                    (
                        "- If a new item overlaps with an existing one, "
                        "update the old item instead of appending a new bullet."
                    ),
                ]
            )
            return "\n".join(lines).rstrip()

        lines.extend(
            [
                f"- Status: {len(self.findings)} duplicate-prone clusters detected.",
                "",
                "## Findings",
            ]
        )
        for index, finding in enumerate(self.findings, start=1):
            lines.extend(
                [
                    f"{index}. **{finding.theme.title}**",
                    f"   - Recommendation: {finding.theme.recommendation}",
                ]
            )
            for item in finding.items:
                preview = " ".join(item.text.split())
                lines.append(
                    f"   - {item.section} L{item.line}: {preview}"
                )
            lines.append("")

        lines.extend(
            [
                "## Guardrails",
                "- Keep one canonical policy per theme.",
                "- `Ключевые решения` = durable policy and architecture.",
                "- `Правила для запоминания` = short operational heuristics only.",
                (
                    "- Move transient debugging details and one-off "
                    "forensics out of `MEMORY.md`."
                ),
            ]
        )
        return "\n".join(lines).rstrip()


class MemoryAuditService:
    """Detect overlapping themes inside MEMORY.md."""

    THEMES: tuple[MemoryTheme, ...] = (
        MemoryTheme(
            key="process_modes",
            title="Processing mode contract duplicated",
            keywords=(
                "/process",
                "/process_full",
                "preview",
                "обработать",
                "full cycle",
                "полный цикл",
            ),
            recommendation=(
                "Keep one canonical policy describing preview/full/nightly entrypoints."
            ),
        ),
        MemoryTheme(
            key="prompt_layer",
            title="Prompt-layer / no-hardcode policy duplicated",
            keywords=(
                "lexical hardcode",
                "keyword hardcode",
                "year-agnostic",
                "context-driven",
                "source-agnostic",
                "owner-assignment",
            ),
            recommendation=(
                "Keep one durable prompt-layer policy and fold "
                "owner-assignment into it unless a separate invariant "
                "is truly needed."
            ),
        ),
        MemoryTheme(
            key="qmd",
            title="qmd operational policy fragmented",
            keywords=(
                "qmd",
                "sqlite",
                "embed",
                "content_vectors",
                "pending_hashes",
                "gethashesforembedding",
                "node_llama_cpp_gpu",
                "rg",
            ),
            recommendation=(
                "Merge into one retrieval policy and one warm-up/validation policy."
            ),
        ),
        MemoryTheme(
            key="content_enrichment",
            title="Link/transcript/language payoff rules duplicated",
            keywords=(
                "content_language",
                "youtube",
                "transcript",
                "short-link",
                "summary",
                "telegram reply",
                "link summary",
            ),
            recommendation=(
                "Keep one intake-enrichment contract covering language, "
                "best-effort enrichment, and immediate user payoff."
            ),
        ),
        MemoryTheme(
            key="mcp_todoist",
            title="Todoist/MCP routing rules duplicated",
            keywords=(
                "mcp_config_path",
                "mcp_servers.json",
                "todoist",
                "inbox",
                "live catalog",
                "structuredcontent.tasks",
            ),
            recommendation=(
                "Keep one routing/config policy and avoid restating "
                "the same Todoist contract in both sections."
            ),
        ),
        MemoryTheme(
            key="source_metadata",
            title="Source metadata contract duplicated",
            keywords=(
                "source_ref",
                "source_url",
                "direct message links",
                "app.plaud.ai/file",
            ),
            recommendation=(
                "Keep one source-metadata contract and remove "
                "the duplicate restatement."
            ),
        ),
        MemoryTheme(
            key="plaud_auth",
            title="PLAUD auth alert rule duplicated",
            keywords=(
                "plaud sync должен один раз уведомлять",
                "plaud auth failure",
                "проблеме авторизации",
                "восстановлении",
                "deduped alert",
            ),
            recommendation=(
                "Keep one short invariant about deduped auth alerts."
            ),
        ),
    )

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path)
        self.memory_path = self.vault_path / "MEMORY.md"

    def audit(self) -> MemoryAuditReport:
        """Inspect MEMORY.md and report overlapping thematic clusters."""
        items = self._extract_items()
        findings: list[MemoryFinding] = []
        for theme in self.THEMES:
            matched = tuple(item for item in items if self._matches_theme(item, theme))
            if len(matched) < 3:
                continue
            findings.append(MemoryFinding(theme=theme, items=matched))
        return MemoryAuditReport(findings=tuple(findings))

    def render(self) -> str:
        """Convenience wrapper used by the processing pipeline."""
        return self.audit().to_markdown()

    def _extract_items(self) -> tuple[MemoryItem, ...]:
        if not self.memory_path.exists():
            return ()
        lines = self.memory_path.read_text(encoding="utf-8").splitlines()
        items: list[MemoryItem] = []
        section = ""
        subsection = ""
        for line_no, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if stripped.startswith("## "):
                section = stripped[3:].strip()
                subsection = ""
                continue
            if stripped.startswith("### "):
                subsection = stripped[4:].strip()
                continue
            if section == "Ключевые решения" and stripped.startswith("**20"):
                items.append(
                    MemoryItem(
                        line=line_no,
                        section="decision",
                        text=self._strip_date_prefix(stripped),
                    )
                )
            elif (
                stripped.startswith("- ")
                and (
                    section == "Правила для запоминания"
                    or (
                        section == "Изученное"
                        and subsection == "Правила для запоминания"
                    )
                )
            ):
                items.append(
                    MemoryItem(
                        line=line_no,
                        section="rule",
                        text=stripped[2:].strip(),
                    )
                )
        return tuple(items)

    @staticmethod
    def _strip_date_prefix(text: str) -> str:
        prefix, marker, rest = text.partition("—")
        if marker:
            return rest.strip()
        return prefix.strip()

    @staticmethod
    def _matches_theme(item: MemoryItem, theme: MemoryTheme) -> bool:
        normalized = item.text.lower()
        return sum(keyword in normalized for keyword in theme.keywords) >= 1
