"""Owner-facing provenance answer for one compiled page (задача L, ТЗ 7.3/7.4).

``build_why``/``build_why_for_path`` are pure functions: they never call a
model and never write to the vault. They only read pages already written
under ``compiled/`` (via ``CompiledBriefingService``) and render one short
Markdown answer to "why does this page say X" from whatever that page
already records -- the same read-only split ``compiled_briefs.py`` uses for
briefs.

What is shown, and the one thing that is deliberately never shown
-------------------------------------------------------------------
Per page: the sources table (by date, from "Sources That Shaped This Page"),
the trust level, the claim-history table (replacements, from "Claim
History"), open conflicts (both sides, from "Open Conflicts"), the date of
the last full-page verification (frontmatter ``last_verified``), the
enrichment count, and whether/when the owner last confirmed the page
(frontmatter ``human_reviewed`` -- a date, not a boolean; see
``compiled_briefings.py``'s own field comment).

Production never records *which* claims a verification pass actually
checked (``compiled_fact_check.py`` bumps only the page-level
``last_verified`` date; ``CompiledBriefingService._render_briefing`` does
the same for a fresh claim). So this module only ever shows the page-level
date with an explicit note that per-claim verification status is not
recorded -- it must never fabricate a claim-by-claim "checked" list, since
no such data exists to fabricate it from.

Target resolution (ТЗ 7.3 "не гадай, спроси")
-----------------------------------------------
Reuses ``compiled_briefs._find_exact_matches`` with ``domain=None`` (search
all six ``COMPILED_BRIEFING_DOMAINS``, unlike a brief's fixed domain) for an
exact slug/path match. A slug is only unique within one domain, so that
search can return several pages at once (``decisions/roadmap``,
``meetings/roadmap``, ``topics/roadmap``); that is an ambiguity like any
other and *all* of them are put to the owner rather than resolved by sort
order or truncated to a pair. Failing that, falls back to
``CompiledBriefingService._rank_candidates`` (limit 2 -- only the top two
ever matter here). ``_rank_candidates`` does not expose its internal
per-candidate score to callers (it only returns an ordered, pre-filtered
list), so "are the top two close enough to ask instead of guess" cannot
reuse its real score. Instead this module computes its own, independent
proxy score per candidate: every *text* term the ranker scores, at the
ranker's own weights -- a title or slug contained whole in the query, plus
the token overlap (via ``CompiledBriefingService._tokens``, already
precedented for cross-module reuse) with that candidate's title,
description, and clipped body. So a pair matched only through their bodies
is still measurable against each other, a passing body mention never ties
with the page the query actually names, and a page whose title *is* the
query outscores one that merely name-drops it. Page metadata (freshness,
confidence, relevance, tier, the domain hint) is left out: it decides which
page ranks higher, not whether the query text is ambiguous. When
a rank-2 candidate exists and its proxy score is at least
``AMBIGUITY_RATIO`` of rank-1's (both counted from the trusted
``_rank_candidates`` order -- this module never re-ranks, only measures how
close the top two are), the result is "ambiguous": the caller must ask the
owner to pick, not guess. ``AMBIGUITY_RATIO = 0.7`` is this module's own
fixed heuristic, not derived from or equal to ``_rank_candidates``'s
internal scoring -- chosen loosely (not tuned against a corpus) as "the
runner-up shares at least 70% as many query terms as the winner", which
tolerates a token or two of difference without over-triggering on every
close pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from d_brain.services.compiled_briefings import (
    MAX_BODY_SNIPPET_CHARS,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)
from d_brain.services.compiled_briefs import (
    _find_exact_matches,
    _sources_block,
    _trust_line,
)

# See module docstring: this module's own ambiguity proxy, independent of
# (and not comparable in magnitude to) _rank_candidates's real score.
AMBIGUITY_RATIO = 0.7


@dataclass(frozen=True, slots=True)
class WhyChoice:
    """One disambiguation option offered to the owner as a button."""

    rel_path: str
    title: str
    domain: str


@dataclass(frozen=True, slots=True)
class WhyResult:
    """A rendered "/why" answer plus what the caller needs to touch/link it."""

    rel_path: str
    title: str
    domain: str
    markdown: str


@dataclass(frozen=True, slots=True)
class WhyOutcome:
    """Outcome of resolving+answering one "/why" query.

    ``status`` is one of:
    - ``"resolved"`` -- ``result`` is set, ``choices`` is empty.
    - ``"ambiguous"`` -- ``choices`` holds every candidate the owner has to
      pick between; ``result`` is ``None``. The caller must ask, not guess
      (ТЗ 7.3). Two for a close lexical near-tie (``_rank_candidates`` is
      called with ``limit=2``, so there is never a third), but *all* of
      them for an exact slug hit across domains, which can be more than two.
    - ``"not_found"`` -- neither ``result`` nor ``choices`` is set.
    """

    status: str
    result: WhyResult | None = None
    choices: tuple[WhyChoice, ...] = ()


def _proxy_score(
    query_lower: str,
    query_tokens: set[str],
    candidate: CompiledBriefingCandidate,
) -> float:
    """This module's own closeness proxy -- see module docstring. Deliberately
    not a reuse of ``_rank_candidates``'s real (unexposed) score.

    Mirrors every *text* term the ranker scores, at the ranker's own
    weights (code review, three times over). Title and description alone
    scored two body-only matches at zero each, the ``top_score > 0`` guard
    below fell through, and /why answered confidently about whichever of
    them sorted first. Counting body tokens as heavily as title tokens then
    overcorrected: a page that merely mentions the query in passing tied
    with the page actually named by it, and /why started asking the owner
    to choose where it used to answer. And weighting the three token
    overlaps alone still left out the two terms that carry the most
    weight of all -- the ranker's +8 for a title the query names outright
    and +5 for the same of a slug -- so a page whose title *is* the
    query could still score under a page that only name-drops it, and /why
    asked about a pair the ranker had ordered decisively. Both bonuses use
    the ranker's own loose containment test, down to its known cost -- see
    the comment on it in ``_rank_candidates``. Tightening it here alone
    would be worse than leaving it: this proxy decides only whether to ask
    the owner or answer, and it would then be asking about an ordering the
    ranker had made on terms the proxy no longer counted.

    Metadata (freshness, confidence, relevance, tier, the domain hint) is
    the one thing deliberately left out: those decide which page ranks
    higher, not whether the query text itself is ambiguous.
    """
    text_score = 0.0
    title_lower = candidate.title.lower()
    if title_lower and title_lower in query_lower:
        text_score += 8.0
    if candidate.slug and candidate.slug.replace("-", " ") in query_lower:
        text_score += 5.0
    for weight, text in (
        (2.4, candidate.title),
        (1.2, candidate.description),
        (
            0.25,
            CompiledBriefingService._clip(
                CompiledBriefingService._body_without_frontmatter(candidate.text),
                MAX_BODY_SNIPPET_CHARS,
            ),
        ),
    ):
        overlap = query_tokens & CompiledBriefingService._tokens(text)
        text_score += weight * len(overlap)
    return text_score


def _to_choice(candidate: CompiledBriefingCandidate) -> WhyChoice:
    return WhyChoice(
        rel_path=candidate.rel_path, title=candidate.title, domain=candidate.domain
    )


def resolve_why_target(
    service: CompiledBriefingService,
    candidates: list[CompiledBriefingCandidate],
    query: str,
) -> tuple[
    str, CompiledBriefingCandidate | None, tuple[CompiledBriefingCandidate, ...]
]:
    """Resolve a "/why" query to one page, an ambiguous set, or nothing.

    Returns ``(status, target, ambiguous)`` -- see ``WhyOutcome`` for what
    each ``status`` means and how large ``ambiguous`` can get.
    ``ambiguous`` is only non-empty when ``status == "ambiguous"``.
    """
    exact = _find_exact_matches(candidates, domain=None, query=query)
    if len(exact) == 1:
        return "resolved", exact[0], ()
    if exact:
        # Same slug in several domains: an exact match that is not a unique
        # one. Every one of them is offered, not just the first two (code
        # review): there are six ``COMPILED_BRIEFING_DOMAINS``, so a slug
        # can land in three or more, and truncating to a pair left the rest
        # unreachable through /why entirely while the owner was told the
        # answer was between two pages. All exact matches are equally exact
        # -- there is no ranking here that could justify dropping one.
        return "ambiguous", None, tuple(exact)

    ranked = service._rank_candidates(query, limit=2)
    if not ranked:
        return "not_found", None, ()
    if len(ranked) == 1:
        return "resolved", ranked[0], ()

    query_lower = (query or "").strip().lower()
    query_tokens = CompiledBriefingService._tokens(query)
    top_score = _proxy_score(query_lower, query_tokens, ranked[0])
    runner_up_score = _proxy_score(query_lower, query_tokens, ranked[1])
    if top_score > 0 and runner_up_score >= top_score * AMBIGUITY_RATIO:
        return "ambiguous", None, (ranked[0], ranked[1])
    return "resolved", ranked[0], ()


def _claim_history_block(text: str) -> list[str]:
    """ТЗ: claim-history table (replacements)."""
    rows = CompiledBriefingService._claim_history_rows(text)
    if not rows:
        return ["- замен не зафиксировано"]
    return [
        f"- {date_value}: было «{claim}» (источник [[{old_source}]]) "
        f"→ заменено [[{superseded_by}]]"
        for date_value, old_source, claim, superseded_by in rows
    ]


def _open_conflicts_block(text: str) -> list[str]:
    """ТЗ: open conflicts (both sides)."""
    rows = CompiledBriefingService._open_conflicts_rows(text)
    if not rows:
        return ["- открытых конфликтов нет"]
    return [
        f"- {since}: «{existing_claim}» (источник [[{existing_source}]]) "
        f"vs «{new_claim}» (источник [[{new_source}]])"
        for since, existing_claim, existing_source, new_claim, new_source in rows
    ]


def _render_why(candidate: CompiledBriefingCandidate) -> str:
    text = candidate.text
    fields = CompiledBriefingService._frontmatter_fields(text)
    last_verified = fields.get("last_verified", "").strip()
    human_reviewed = fields.get("human_reviewed", "").strip()
    enrichment_count = CompiledBriefingService._int_value(
        fields.get("enrichment_count"), default=0
    )

    lines = [
        f"**Почему страница говорит так — {candidate.title}**",
        _trust_line(text),
        "",
        "**Источники (по датам)**",
        *_sources_block(text),
        "",
        "**История утверждений (замены)**",
        *_claim_history_block(text),
        "",
        "**Открытые конфликты**",
        *_open_conflicts_block(text),
        "",
        "**Проверка страницы**",
        (
            f"Последняя полная проверка: {last_verified or 'не зафиксирована'}. "
            "Какие именно утверждения проверялись — не записывается; "
            "хранится только дата проверки всей страницы."
        ),
        f"Подтверждение владельцем: {human_reviewed or 'не подтверждалось'}.",
        f"Число проходов обогащения: {enrichment_count}.",
    ]
    return "\n".join(lines)


def build_why_for_path(vault_path: Path, rel_path: str) -> WhyResult | None:
    """Render the "/why" answer for a page whose path is already known --
    used both by ``build_why`` after resolution and by the bot's
    disambiguation callback (the owner already picked one of the two
    offered choices, so no re-resolution is needed)."""
    service = CompiledBriefingService(Path(vault_path))
    for candidate in service._iter_candidates():
        if candidate.rel_path == rel_path:
            return WhyResult(
                rel_path=candidate.rel_path,
                title=candidate.title,
                domain=candidate.domain,
                markdown=_render_why(candidate),
            )
    return None


def build_why(vault_path: Path, query: str) -> WhyOutcome:
    """Resolve ``query`` to a compiled page and render its "/why" answer
    (задача L, ТЗ 7.3). Pure and read-only: reads ``compiled/**`` pages
    already on disk, calls no model, writes nothing.
    """
    service = CompiledBriefingService(Path(vault_path))
    candidates = service._iter_candidates()
    status, target, ambiguous_pair = resolve_why_target(service, candidates, query)

    if status == "not_found":
        return WhyOutcome(status="not_found")
    if status == "ambiguous":
        return WhyOutcome(
            status="ambiguous",
            choices=tuple(_to_choice(candidate) for candidate in ambiguous_pair),
        )
    assert target is not None
    result = WhyResult(
        rel_path=target.rel_path,
        title=target.title,
        domain=target.domain,
        markdown=_render_why(target),
    )
    return WhyOutcome(status="resolved", result=result)
