"""Deterministic provenance for direct-question answers (ТЗ 7.4, 4.4).

``build_question_provenance`` is a pure, read-only function: it never calls a
model and never writes to the vault. It only re-runs the exact lexical
ranking ``CompiledBriefingService.build_question_context`` already used to
pick which compiled pages go into the answer prompt
(``CompiledBriefingService._rank_candidates``), then reads each ranked
page's own frontmatter (``sources_trust``, ``conflicts_open``) to render a
short, code-built "Источники ответа" block plus an optional weak-trust
warning. Trust is a code-determined fact (ТЗ 4.4): this module never parses
or trusts the model's own free-text "Источники:" list, and that list is
left completely untouched -- it still carries provenance for non-compiled
sources (``daily/``, ``thoughts/``, ``imports/``) that have no frontmatter
to read trust from.

Honesty note (explicitly required by this task's plan): "pages ranked here"
is not the same claim as "pages the model actually drew on to write its
answer". There is no reliable way to recover that after the fact without
either asking the model to self-report (which ТЗ 4.4 already rules out for
trust) or parsing its free-text source list (which this module deliberately
does not do). What is shown is accurate for the *real* candidate page it
names -- a re-attribution, not a fabrication -- but it is best read as "the
pages most likely behind this answer", not a verified usage log.

Re-running the ranking after the model has already answered is cheap and
side-effect free: a filesystem walk over ``compiled/**`` plus lexical
scoring, no network or model call -- but it is also stale by construction:
the vault can change while the model is working (background enrichment, or
the model's own writes), so a fresh re-rank can read different frontmatter
than what was actually in the prompt. Callers that already ranked
candidates when they built the prompt (see ``CliProcessor`` in
``processor.py``) should pass them back in via the ``candidates`` keyword
below instead of relying on the built-in re-rank (ТЗ 7.4 code-review
defect 2). The re-rank fallback stays in place for direct/standalone
callers, e.g. the tests in this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from d_brain.services.compiled_briefings import (
    QUESTION_CONTEXT_LIMIT,
    SOURCES_TRUST_VALUES,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)

# Reused, not duplicated: this is the same weak-trust set `compiled_briefs`
# uses for its own enrichment-report warnings (ТЗ 4.3/4.4). Provenance and
# enrichment must agree on what "weak" means, so importing the private
# constant is intentional -- keeping a second copy would let the two drift.
from d_brain.services.compiled_briefs import _LOW_TRUST_LEVELS

_UNKNOWN_TRUST_LABEL = "не определено"


@dataclass(frozen=True, slots=True)
class QuestionProvenance:
    """Deterministic provenance for one answered question (ТЗ 7.4).

    ``warning`` is a short callout for a weak-trust or open-conflict
    situation, meant to be inserted right after the answer's first *real*
    paragraph -- never before it, and never wedged between an opening
    heading/table/code-fence and that paragraph (code-review defect 1),
    since the model is only asked, not guaranteed, to open with prose (see
    the caller, ``CliProcessor._insert_after_first_paragraph`` in
    ``processor.py``). ``block`` is the full per-page breakdown, meant to
    be appended at the end of the message.
    ``touched_paths`` are the compiled pages that should receive a memory
    ``touch`` (ТЗ 6.2/7.4) for having backed this answer.

    Every field is empty when no compiled page ranked for the question --
    callers must render nothing at all, not a placeholder (this project's
    existing convention: empty blocks are omitted, never stubbed).
    """

    warning: str
    block: str
    touched_paths: tuple[str, ...]


def _page_trust(candidate: CompiledBriefingCandidate) -> str:
    """ТЗ 4.4: trust is a frontmatter fact, read by code only -- never
    guessed and never taken from the model's own claims."""
    fields = CompiledBriefingService._frontmatter_fields(candidate.text)
    trust = fields.get("sources_trust", "").strip()
    return trust if trust in SOURCES_TRUST_VALUES else ""


def _page_conflicts(candidate: CompiledBriefingCandidate) -> int:
    fields = CompiledBriefingService._frontmatter_fields(candidate.text)
    return CompiledBriefingService._int_value(fields.get("conflicts_open"), default=0)


def _fold_trust(levels: list[str]) -> str:
    """Weakest trust across the shown pages (ТЗ 4.3), folded with the same
    minimum used to compute a page's own ``sources_trust``. Pages that have
    not been enriched yet (empty trust) are skipped rather than folded in --
    an unset field must not drag a real page's trust down to a false alarm.
    """
    known = [level for level in levels if level]
    if not known:
        return ""
    folded = known[0]
    for level in known[1:]:
        folded = CompiledBriefingService._min_trust(folded, level)
    return folded


def _weak_trust_warning(*, folded_trust: str, any_open_conflicts: bool) -> str:
    """One visible callout (ТЗ 4.4 "Правила" + this task's plan), triggered
    by the folded trust being ``forwarded``/``inferred``, by any shown page
    carrying an open conflict, or both at once."""
    reasons = []
    if folded_trust in _LOW_TRUST_LEVELS:
        reasons.append(f"уровень доверия источников — «{folded_trust}»")
    if any_open_conflicts:
        reasons.append("на одной из страниц есть неразрешённый конфликт фактов")
    if not reasons:
        return ""
    return (
        f"**Внимание: {'; '.join(reasons)}.** "
        "Часть утверждений не проверена напрямую владельцем."
    )


def _page_line(candidate: CompiledBriefingCandidate, trust: str, conflicts: int) -> str:
    """One compact row: aliased wikilink (matches the digest's own link
    format), trust label, and open-conflict count when present."""
    trust_label = trust or _UNKNOWN_TRUST_LABEL
    line = f"- [[{candidate.rel_path}|{candidate.title}]] — доверие: {trust_label}"
    if conflicts:
        line += f", открытых конфликтов: {conflicts}"
    return line


def build_question_provenance(
    vault_path: Path,
    question: str,
    *,
    limit: int = QUESTION_CONTEXT_LIMIT,
    candidates: Sequence[CompiledBriefingCandidate] | None = None,
) -> QuestionProvenance:
    """Pure, read-only rebuild of ``build_question_context``'s own ranking
    (ТЗ 7.4), used to attach code-determined provenance after the model has
    already produced its answer. See the module docstring for exactly what
    "ranked here" does and does not claim about actual model usage.

    ``candidates``, when given, are used exactly as passed instead of
    re-ranking against the vault (ТЗ 7.4 code-review defect 2): the caller
    already ranked these pages -- and this function will read frontmatter
    off the exact same in-memory ``CompiledBriefingCandidate.text`` values
    it captured -- at prompt-build time, before the model ran and before
    anything on disk could have changed underneath it. An empty sequence
    means "the caller built no compiled-pages block for this question", so
    it produces the same empty result as ranking finding nothing (ТЗ 7.4
    code-review defect 3). ``None`` (the default) keeps the old
    self-contained re-ranking behavior for callers with no prior ranking to
    reuse.
    """
    if candidates is None:
        service = CompiledBriefingService(Path(vault_path))
        candidates = service._rank_candidates(question, limit=limit)
    if not candidates:
        return QuestionProvenance(warning="", block="", touched_paths=())

    rows = [
        (candidate, _page_trust(candidate), _page_conflicts(candidate))
        for candidate in candidates
    ]
    folded_trust = _fold_trust([trust for _, trust, _ in rows])
    any_open_conflicts = any(conflicts > 0 for _, _, conflicts in rows)

    page_lines = [
        _page_line(candidate, trust, conflicts) for candidate, trust, conflicts in rows
    ]
    block = "\n".join(["**Источники ответа**", *page_lines])
    warning = _weak_trust_warning(
        folded_trust=folded_trust, any_open_conflicts=any_open_conflicts
    )
    touched_paths = tuple(candidate.rel_path for candidate, _, _ in rows)
    return QuestionProvenance(warning=warning, block=block, touched_paths=touched_paths)
