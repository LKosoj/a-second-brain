"""Owner-facing briefs assembled from already-compiled pages (ТЗ 7.6).

``build_brief`` is a pure function: it never calls a model and never writes
to the vault. It only reads pages already written under ``compiled/`` (via
``CompiledBriefingService``) and renders one short Markdown brief from
whatever that single page already records. Callers (the CLI) persist the
result to ``summaries/briefs/YYYY-MM-DD-<type>-<slug>.md`` and deliver it to
Telegram -- the same read/write split ``compiled_enrich_report.py`` uses for
the daily digest, kept here for the same reason (this module stays a plain
reader, independent of the write path's own environment quirks).

Provenance is mandatory (ТЗ 7.6, pointing at ТЗ 4.4/7.4): every rendered
claim keeps the source it was recorded with, and the page's own trust level
(frontmatter ``sources_trust``) is shown right under the brief's title --
not at the bottom -- with an explicit callout when it is ``forwarded`` or
``inferred`` (ТЗ 4.4: those two levels alone must never look as trustworthy
as ``own``/``integration``).

Target resolution (ТЗ 7.6 step 1, "a clear result, not a file or an
exception" when nothing matches): exact slug or vault-relative path match
inside the brief type's own domain first, then the best
``CompiledBriefingService._rank_candidates`` hit restricted to that same
domain. The domain restriction on the fallback is this implementation's own
choice, not stated verbatim in the ТЗ (only the exact-match step spells out
"in the right domain") -- see the task report: without it, a "decision"
brief could resolve to a ``topics``/``projects`` page that has none of the
decision-only fields (``decision_date``, "Rationale", "Alternatives
Considered"), which would silently render an empty decision brief instead
of a useful one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from d_brain.services.compiled_briefings import (
    SOURCES_TRUST_VALUES,
    WIKILINK_RE,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)

# ТЗ 7.6: "решение, тема, проект" -- domain mapping is fixed by the ТЗ 4.1
# compiled/ domain list, not configurable.
BRIEF_TYPES = ("decision", "topic", "project")
_BRIEF_DOMAIN = {"decision": "decisions", "topic": "topics", "project": "projects"}
_BRIEF_LABEL = {"decision": "по решению", "topic": "по теме", "project": "по проекту"}

# ТЗ 4.4 "Правила": these two trust levels must never be shown quietly --
# the owner must see the callout, not a small-print footnote.
_LOW_TRUST_LEVELS = frozenset({"forwarded", "inferred"})


@dataclass(frozen=True, slots=True)
class BriefResult:
    """A rendered brief plus what the CLI needs to file and deliver it."""

    brief_type: str
    domain: str
    slug: str
    source_rel_path: str
    title: str
    markdown: str


def _find_exact_matches(
    candidates: list[CompiledBriefingCandidate],
    *,
    domain: str | None,
    query: str,
) -> list[CompiledBriefingCandidate]:
    """ТЗ 7.6 step 1, first half: every exact slug or path match in ``domain``.

    ``domain=None`` searches all candidates regardless of domain -- used by
    ``/why`` (задача L, ТЗ 7.3), which has no fixed target domain the way a
    brief type does (``_BRIEF_DOMAIN``); a brief call always still passes a
    concrete domain string.

    Returns a list, not one candidate, because a slug is only unique *within*
    a domain: ``compiled/decisions/roadmap.md`` and
    ``compiled/meetings/roadmap.md`` both answer to the slug ``roadmap``, and
    a cross-domain caller must ask the owner which one they meant instead of
    taking whichever page sorts first (``resolve_why_target``). A path match
    is unique by construction, so the returned list is then always length 1.
    """
    query_norm = (query or "").strip()
    if not query_norm:
        return []
    path_query = query_norm.removeprefix("vault/")
    in_domain = (
        candidates
        if domain is None
        else [candidate for candidate in candidates if candidate.domain == domain]
    )
    for candidate in in_domain:
        if candidate.rel_path == path_query:
            return [candidate]
    slug_query = CompiledBriefingService._slugify(query_norm)
    if not slug_query:
        return []
    return [candidate for candidate in in_domain if candidate.slug == slug_query]


def _find_exact_target(
    candidates: list[CompiledBriefingCandidate],
    *,
    domain: str | None,
    query: str,
) -> CompiledBriefingCandidate | None:
    """The first exact match from ``_find_exact_matches`` -- for callers that
    pass a concrete ``domain`` and therefore cannot hit a cross-domain slug
    collision."""
    matches = _find_exact_matches(candidates, domain=domain, query=query)
    return matches[0] if matches else None


def _find_target(
    service: CompiledBriefingService,
    candidates: list[CompiledBriefingCandidate],
    *,
    domain: str,
    query: str,
) -> CompiledBriefingCandidate | None:
    """ТЗ 7.6 step 1 in full: exact match first, else the best ranked hit
    restricted to ``domain`` (see module docstring for why the fallback is
    also domain-restricted here)."""
    target = _find_exact_target(candidates, domain=domain, query=query)
    if target is not None:
        return target
    # Restrict the field before ranking, not after (code review): the
    # ranker's ``min_score`` cut is relative to the best page it saw, so
    # ranking the whole vault and filtering the result let a strong page in
    # another domain push a genuinely relevant page in ``domain`` under the
    # bar -- and this function then reported "не нашёл" for a page that
    # exists.
    ranked = service._rank_candidates(query, limit=1, domain=domain)
    return ranked[0] if ranked else None


def _trust_line(text: str) -> str:
    """ТЗ 7.6 / 4.4: the page's trust level, shown right under the title --
    an explicit callout for ``forwarded``/``inferred``, not small print."""
    trust = CompiledBriefingService._frontmatter_fields(text).get(
        "sources_trust", ""
    ).strip()
    if trust not in SOURCES_TRUST_VALUES:
        return (
            "Уровень доверия источников страницы: не определён "
            "(страница ещё не обогащалась)."
        )
    if trust in _LOW_TRUST_LEVELS:
        return (
            f"**Внимание: уровень доверия источников страницы — «{trust}».** "
            "Часть утверждений не проверена напрямую владельцем."
        )
    return f"Уровень доверия источников страницы: {trust}."


def _sources_block(text: str) -> list[str]:
    """Provenance for every claim (ТЗ 7.6): the per-addition "Sources That
    Shaped This Page" table already carries a date, a source and what was
    added for each entry -- prefer it. Fall back to the page's plain
    "## Sources" link list only when that table is empty (a page compiled
    before it existed, or a page with a torn-up section), so sources are
    surfaced rather than silently dropped.
    """
    rows = CompiledBriefingService._sources_shaped_rows(text)
    if rows:
        return [
            f"- {date_value} · [[{source_value}]] — {what_value}"
            for date_value, source_value, what_value in rows
        ]
    section = CompiledBriefingService._section_text(text, "Sources")
    links = [path.strip() for path in WIKILINK_RE.findall(section) if path.strip()]
    if links:
        return [f"- [[{path}]]" for path in links]
    return ["- источники не зафиксированы"]


def _supersedes_block(target: CompiledBriefingCandidate) -> list[str]:
    """ТЗ 7.6: what this decision invalidates.

    Originally implemented as a reverse lookup over "Claim History" rows
    across the vault, matching on the "Superseded By" column. That column
    never actually holds a compiled decision page's own path: production
    code (``CompiledBriefingService._apply_claims_and_conflicts``) always
    writes the *source* that triggered the supersession there (a
    ``daily/...``/``thoughts/...`` path), confirmed by rendering a page
    through the real ``_render_briefing`` and inspecting the row -- see the
    task report. That made this section permanently empty.

    The frontmatter this decision page carries its own answer in:
    ``supersedes`` (ТЗ 5.3 schema: a list of "decision identifier" strings).
    Confirmed the same way: it is free-form model text, not a vault path
    (``CompiledBriefingService._apply_claims_and_conflicts`` docstring says
    so explicitly, and a rendered page's frontmatter bears it out), so it
    cannot be turned into a [[wikilink]] -- shown as plain text with an
    explicit note instead of a link that would silently be wrong.
    """
    raw = CompiledBriefingService._frontmatter_fields(target.text).get(
        "supersedes", ""
    )
    try:
        parsed = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        parsed = []
    items = (
        [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, list)
        else []
    )
    if not items:
        return ["- не зафиксировано"]
    return [
        f"- «{item}» (текст со страницы решения, не ссылка на страницу)"
        for item in items
    ]


def _decision_brief(target: CompiledBriefingCandidate) -> str:
    """ТЗ 7.6: суть, когда принято, обоснование, отклонённые варианты, что
    отменяет, источники."""
    fields = CompiledBriefingService._frontmatter_fields(target.text)
    rationale = CompiledBriefingService._section_text(target.text, "Rationale")
    alternatives = CompiledBriefingService._section_bullets(
        target.text, "Alternatives Considered"
    )
    lines = [
        f"**Бриф по решению — {target.title}**",
        _trust_line(target.text),
        "",
        "**В чём суть**",
        target.description or "не зафиксировано",
        "",
        "**Когда принято**",
        fields.get("decision_date") or "не зафиксировано",
        "",
        "**Обоснование**",
        rationale or "не зафиксировано",
        "",
        "**Отклонённые варианты**",
        *([f"- {item}" for item in alternatives] or ["- не зафиксированы"]),
        "",
        "**Что это решение отменяет**",
        *_supersedes_block(target),
        "",
        "**Источники**",
        *_sources_block(target.text),
    ]
    return "\n".join(lines)


def _conflict_lines(target: CompiledBriefingCandidate) -> list[str]:
    """Shared by ``_topic_brief``/``_project_brief``/``_decision_brief``-adjacent
    callers: ТЗ 7.6 doesn't list "открытые конфликты" by name for topic/project
    (only decision's list omits it too), but the page already tracks both
    sides with sources, and dropping a live conflict from a brief would hide
    exactly the kind of thing a brief exists to surface -- kept for both
    brief types, same rendering as before this defect's fix."""
    conflicts = CompiledBriefingService._open_conflicts_rows(target.text)
    return [
        f"- {since}: «{existing_claim}» (источник [[{existing_source}]]) "
        f"vs «{new_claim}» (источник [[{new_source}]])"
        for since, existing_claim, existing_source, new_claim, new_source in conflicts
    ] or ["- открытых конфликтов нет"]


def _topic_brief(target: CompiledBriefingCandidate) -> str:
    """ТЗ 7.6: "текущее состояние, ключевые решения, открытые вопросы,
    участники".

    "Участники" is deliberately not rendered here (not even as an empty
    placeholder): no field on a compiled page carries who is involved.
    Checked the full model schema (``COMPILE_JSON_EXAMPLE`` in
    ``compiled_briefings.py``) and the public schema doc
    (``resources/project_template/skills/compile-enrich/references/
    page-schema.md``) -- neither has a participants field, and no other
    section on the page records it either. Rendering "- нет" would claim the
    page was checked for participants and none were found, which is false;
    the page never tracks the concept at all -- "пустой раздел хуже
    отсутствующего", see the task report.
    """
    current_state = CompiledBriefingService._section_text(target.text, "Current State")
    key_decisions = CompiledBriefingService._section_bullets(
        target.text, "Key Decisions"
    )
    open_loops = CompiledBriefingService._section_bullets(target.text, "Open Loops")
    lines = [
        f"**Бриф {_BRIEF_LABEL['topic']} — {target.title}**",
        _trust_line(target.text),
        "",
        "**Текущее состояние**",
        current_state or "не зафиксировано",
        "",
        "**Ключевые решения**",
        *([f"- {item}" for item in key_decisions] or ["- нет"]),
        "",
        "**Открытые вопросы**",
        *([f"- {item}" for item in open_loops] or ["- нет"]),
        "",
        "**Открытые конфликты**",
        *_conflict_lines(target),
        "",
        "**Источники**",
        *_sources_block(target.text),
    ]
    return "\n".join(lines)


def _project_brief(target: CompiledBriefingCandidate) -> str:
    """ТЗ 7.6: "состояние, недавние изменения, обязательства, риски".

    "Обязательства" and "риски" are deliberately not rendered here (not even
    as empty placeholders), same reasoning as ``_topic_brief``'s missing
    "участники" -- no field on a compiled page carries either:

    - "Обязательства": a claim is tagged ``kind: commitment`` only
      transiently during compilation (ТЗ 5.3); ``CompiledBriefingService.
      _apply_claims_and_conflicts`` uses that tag purely for significance/
      conflict gating and never writes it to the page -- only
      ``claim["text"]``/``claim["source"]`` survive into "Sources That
      Shaped This Page" (confirmed by reading that method). A rendered page
      cannot tell a commitment claim from a plain fact after the fact.
    - "Риски": no field anywhere in ``COMPILE_JSON_EXAMPLE`` or
      ``page-schema.md`` records risk at all.

    See the task report for both checks in full.
    """
    current_state = CompiledBriefingService._section_text(target.text, "Current State")
    recent_changes = CompiledBriefingService._section_bullets(
        target.text, "Recent Changes"
    )
    lines = [
        f"**Бриф {_BRIEF_LABEL['project']} — {target.title}**",
        _trust_line(target.text),
        "",
        "**Состояние**",
        current_state or "не зафиксировано",
        "",
        "**Недавние изменения**",
        *([f"- {item}" for item in recent_changes] or ["- нет"]),
        "",
        "**Открытые конфликты**",
        *_conflict_lines(target),
        "",
        "**Источники**",
        *_sources_block(target.text),
    ]
    return "\n".join(lines)


def build_brief(vault_path: Path, *, brief_type: str, query: str) -> BriefResult | None:
    """Assemble one owner brief (ТЗ 7.6) from an existing compiled page.

    Returns ``None`` -- not an exception, and nothing is written -- when
    nothing matches (ТЗ 7.6 step 1: "a clear result, not a file or an
    exception"). Raises ``ValueError`` for a ``brief_type`` outside
    ``BRIEF_TYPES``; the CLI never produces one (``argparse`` choices
    enforce it), so this only guards direct callers.
    """
    if brief_type not in _BRIEF_DOMAIN:
        raise ValueError(f"unknown brief type: {brief_type!r}")
    domain = _BRIEF_DOMAIN[brief_type]
    service = CompiledBriefingService(Path(vault_path))
    candidates = service._iter_candidates()
    target = _find_target(service, candidates, domain=domain, query=query)
    if target is None:
        return None

    if brief_type == "decision":
        markdown = _decision_brief(target)
    elif brief_type == "topic":
        markdown = _topic_brief(target)
    else:
        markdown = _project_brief(target)

    return BriefResult(
        brief_type=brief_type,
        domain=domain,
        slug=target.slug,
        source_rel_path=target.rel_path,
        title=target.title,
        markdown=markdown,
    )


def render_brief_note(result: BriefResult, today: date) -> bytes:
    """Wrap the brief body in the frontmatter the ``derived`` profile
    requires for anything under ``summaries/`` (``frontmatter.route_profile``
    routes that path to ``derived``: type/description/last_accessed/
    relevance/tier).

    Extracted from ``run_compiled_brief.py``'s former private ``_render_note``
    (задача L) so both the CLI and the bot's "Собрать бриф" button call the
    same rendering code; ``run_compiled_brief.py`` now imports this under its
    old private name so its own tests keep working unchanged.
    """
    description = f"Бриф {_BRIEF_LABEL[result.brief_type]}: {result.title}"
    header = (
        "---\n"
        "type: compiled-brief\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"last_accessed: {today.isoformat()}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
    )
    return (header + result.markdown.strip() + "\n").encode("utf-8")


def brief_path(vault_path: Path, result: BriefResult, today: date) -> Path:
    """ТЗ 7.6 file name; a same-day collision gets a counter suffix (same
    pattern as ``CompiledBriefingService.file_output_artifact``).

    Callers writing the result must call this only while holding the vault
    write lock -- the ``exists()`` check below is a plain filesystem read,
    not atomic on its own. Extracted from ``run_compiled_brief.py``'s former
    private ``_brief_path`` (задача L); see ``render_brief_note`` docstring.
    """
    directory = vault_path / "summaries" / "briefs"
    base = f"{today.isoformat()}-{result.brief_type}-{result.slug}"
    path = directory / f"{base}.md"
    counter = 1
    while path.exists():
        path = directory / f"{base}-{counter}.md"
        counter += 1
    return path
