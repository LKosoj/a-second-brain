"""Monthly deterministic fact-check for the compiled layer (ТЗ 6.6).

Independent of, and much narrower than, the compile-enrich pass in
``compiled_briefings.py``: it never calls a model, never rewrites a page
body, and only ever patches two frontmatter fields -- ``last_verified`` and
``confidence`` -- via ``patch_validated_vault_frontmatter``. The existing
per-claim ``last_verified`` bump inside ``CompiledBriefingService.
_render_briefing`` (set whenever a brand-new claim is appended during
enrichment) is a separate mechanism and is untouched by this module.

The sampling unit is the *page*, not the claim (ТЗ 6.6): once a page is
picked, every live claim on it -- one row of its "Sources That Shaped This
Page" table -- is re-checked. The 100%/25%-by-tier Verify sampling ratio
(ТЗ 5.6) governs enrichment-time Verify only and does not apply here.

Claim classification is heuristic (the original ``kind`` used during
enrichment is not persisted anywhere), computed primarily from the row's
own Source column rather than the claim text: every row in a live "Sources
That Shaped This Page" table has a Source (compiled_briefings.py always
fills it from the excerpt/day file being processed, never from the model),
so a claim whose Source no longer resolves under the vault (deleted,
renamed, moved) has lost its footing and is "environment-dependent"/failed,
resolved with the same technique (not the same call) as
``CompiledBriefingService._resolve_lint_source_path``. Any wikilink that
additionally appears in the claim text is folded into the same check and
must resolve too -- the compile prompt never asks the model to put one
there, so this is a defensive extra, not the primary signal. A claim that
reads like a reference to a Todoist task is checked first, before the
Source check, and is always "unverifiable" regardless of its source --
there is no Todoist client here and no claim -> task-id mapping in this
version, an outcome the ТЗ explicitly sanctions rather than a shortfall.
Everything else -- in practice only a row whose Source text is blank,
which the current table format cannot even round-trip through its parser
-- falls back to "judgment", counted as passing only while the page
currently has zero open conflicts. That page-level (not per-claim) verdict
for judgment claims, like the deliberately broad Todoist-mention regex
above, is a known simplification, kept as-is rather than reworked.

A page's ``last_verified`` advances to today only when at most half of its
*checked* claims (environment-dependent + judgment; unverifiable claims are
never checked, so they are excluded from this ratio) failed -- the same
"more than half fails" principle ``compiled_briefings._verify_claims_batch``
uses for enrichment-time Verify rejection (``rejected_count * 2 >
len(sample)``), mirrored here rather than reused as a call. A page that
fails this gate keeps its previous ``last_verified`` and is appended to
``.session/decisions-queue.json`` for the owner to decide on archival --
this module never archives a page itself.

Confidence effects (ТЗ 6.6) combine: any unverifiable claim caps the result
at "medium" (never "high"); failing the half-gate steps confidence down one
level (high -> medium -> low -> low). Both can apply to the same page.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefings import (
    CONFIDENCE_VALUES,
    DATE_ONLY_RE,
    WIKILINK_RE,
    CompiledBriefingCandidate,
    CompiledBriefingService,
    _atomic_write_text,
)
from d_brain.services.decisions_queue import append_decision_queue_entries
from d_brain.services.frontmatter import patch_validated_vault_frontmatter
from d_brain.services.vault_lock import vault_write_lock

logger = logging.getLogger(__name__)

# ТЗ 6.6: only pages in active use are re-verified; "cold"/"archive" pages
# are never enriched either (ТЗ 6.1), so checking them here would just burn
# the nightly budget on pages nobody currently relies on. A page with no
# tier yet (the memory-decay engine has not scored it) is eligible too --
# see ``select_pages_for_fact_check`` -- so it is not silently skipped
# forever just for lacking a score.
FACT_CHECK_ELIGIBLE_TIERS = frozenset({"core", "active", "warm"})
# "Небольшая константа за прогон" (ТЗ 6.6). At a few hundred compiled
# pages, ~20/night still revisits every page roughly monthly.
DEFAULT_FACT_CHECK_PAGE_LIMIT = 20

CLAIM_CATEGORY_ENVIRONMENT = "environment"
CLAIM_CATEGORY_JUDGMENT = "judgment"
CLAIM_CATEGORY_UNVERIFIABLE = "unverifiable"

_CONFIDENCE_ORDER = ("low", "medium", "high")

# Coarse keyword heuristic for "this claim is about a Todoist task" (ТЗ
# 6.6). Deliberately permissive: a false positive only routes a claim to
# "unverifiable" (never fails it), so erring toward this bucket is safe.
_TASK_REFERENCE_HINT_RE = re.compile(r"todoist|задач", re.IGNORECASE)

# Own journal, separate from compile-enrich's ``.session/compile-enrich.json``
# -- that file is overwritten whole by every enrichment pass (ТЗ 5.2 step
# 6), so sharing it here would let one process erase the other's record.
_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-fact-check.json"
# Free-form (ТЗ 7.2's queue "kind" is documented as free-form); kept
# distinct from enrichment-time Verify's own "verify-rejected" label so the
# digest and any future owner tooling can tell the two processes apart.
_DECISION_KIND = "fact-check-rejected"


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """One classified, checked claim from a page's "Sources That Shaped
    This Page" table."""

    text: str
    category: str  # environment | judgment | unverifiable
    passed: bool | None  # None only for "unverifiable" (never checked)


@dataclass(frozen=True, slots=True)
class PageFactCheckPlan:
    """Deterministic Verify outcome for one page, computed without any
    disk write (see ``evaluate_page``). ``run_monthly_fact_check`` turns
    this into an actual frontmatter patch and, on gate failure, a
    decisions-queue entry.
    """

    rel_path: str
    claims: tuple[ClaimVerdict, ...]
    checked_count: int
    failed_count: int
    unverifiable_count: int
    exceeded_half: bool
    previous_last_verified: str
    next_last_verified: str
    previous_confidence: str
    next_confidence: str

    @property
    def frontmatter_updates(self) -> dict[str, str]:
        updates: dict[str, str] = {}
        if self.next_last_verified != self.previous_last_verified:
            updates["last_verified"] = self.next_last_verified
        if self.next_confidence != self.previous_confidence:
            updates["confidence"] = self.next_confidence
        return updates


def _resolve_vault_wikilink(vault_path: Path, raw_link: str) -> Path | None:
    """Resolve one ``[[...]]`` target to a file under ``vault_path``, or
    ``None`` if it cannot be found.

    Mirrors the technique ``CompiledBriefingService._resolve_lint_source_
    path`` uses (strip a leading ``vault/`` and any ``#anchor``, try the
    literal path, then a ``.md`` sibling, and stay inside the vault root)
    without calling it: that helper is scoped to lint's own "source"
    column, not to arbitrary claim text.
    """
    text = str(raw_link or "").split("#", 1)[0].strip()
    if text.startswith("vault/"):
        text = text[len("vault/") :]
    if not text or Path(text).is_absolute():
        return None
    candidate = (vault_path / text).resolve()
    try:
        candidate.relative_to(vault_path)
    except ValueError:
        return None
    if candidate.exists():
        return candidate
    if not Path(text).suffix:
        md_candidate = candidate.with_suffix(".md")
        if md_candidate.exists():
            return md_candidate
    return None


def _claim_category(
    vault_path: Path, source: str, claim_text: str
) -> tuple[str, bool | None]:
    """Classify one claim from a "Sources That Shaped This Page" row.

    A task-ish claim is checked first and always wins that bucket
    regardless of its source (see module docstring). Otherwise the row's
    Source column is the primary, always-populated signal for
    "environment-dependent"; any wikilink additionally present in the claim
    text is checked too and must resolve as well, so a second reference
    cannot paper over a first, broken one. Judgment claims come back with
    ``passed=None`` -- their verdict depends on the page's open-conflict
    state, decided by the caller (``evaluate_page``), not here.
    """
    if _TASK_REFERENCE_HINT_RE.search(claim_text):
        return CLAIM_CATEGORY_UNVERIFIABLE, None
    links = [source.strip()] if source.strip() else []
    links += [
        match.strip() for match in WIKILINK_RE.findall(claim_text) if match.strip()
    ]
    if links:
        passed = all(
            _resolve_vault_wikilink(vault_path, link) is not None for link in links
        )
        return CLAIM_CATEGORY_ENVIRONMENT, passed
    return CLAIM_CATEGORY_JUDGMENT, None


def _step_down_confidence(value: str) -> str:
    index = _CONFIDENCE_ORDER.index(value)
    return _CONFIDENCE_ORDER[max(0, index - 1)]


def _cap_confidence_at_medium(value: str) -> str:
    if _CONFIDENCE_ORDER.index(value) > _CONFIDENCE_ORDER.index("medium"):
        return "medium"
    return value


def evaluate_page(
    vault_path: Path,
    candidate: CompiledBriefingCandidate,
    *,
    today: date,
) -> PageFactCheckPlan:
    """Deterministically re-check every live claim on one page (ТЗ 6.6).

    Pure: reads only ``candidate.text`` (already loaded by
    ``_iter_candidates``) plus the filesystem for wikilink targets. No
    frontmatter is written here -- see ``run_monthly_fact_check``.
    """
    fields = CompiledBriefingService._frontmatter_fields(candidate.text)
    conflicts_open = CompiledBriefingService._int_value(
        fields.get("conflicts_open"), default=0
    )
    has_open_conflicts = conflicts_open > 0 or bool(
        CompiledBriefingService._open_conflicts_rows(candidate.text)
    )

    verdicts: list[ClaimVerdict] = []
    for _row_date, source, claim_text in CompiledBriefingService._sources_shaped_rows(
        candidate.text
    ):
        claim_text = claim_text.strip()
        if not claim_text:
            continue
        category, passed = _claim_category(vault_path, source, claim_text)
        if category == CLAIM_CATEGORY_JUDGMENT:
            passed = not has_open_conflicts
        verdicts.append(
            ClaimVerdict(text=claim_text, category=category, passed=passed)
        )

    checked = [
        item for item in verdicts if item.category != CLAIM_CATEGORY_UNVERIFIABLE
    ]
    failed = [item for item in checked if item.passed is False]
    unverifiable = [
        item for item in verdicts if item.category == CLAIM_CATEGORY_UNVERIFIABLE
    ]

    # Same "strictly more than half fails" gate compiled_briefings.py's
    # Verify step uses (``_verify_claims_batch``:
    # ``rejected_count * 2 > len(sample)``) -- matching principle, separate
    # call, over this page's checked claims instead of an enrichment
    # sample. An empty ``checked`` list passes trivially (0 > 0 is False),
    # same as an empty Verify sample would.
    exceeded_half = len(failed) * 2 > len(checked)

    previous_last_verified = fields.get("last_verified", "").strip()
    if not DATE_ONLY_RE.match(previous_last_verified):
        previous_last_verified = ""
    # "Nothing was checked" must not look like "everything passed": unlike
    # the enrichment-time Verify pattern this gate borrows from (where an
    # empty sample means a page with no claims yet), an empty ``checked``
    # here means every claim on this page was skipped (e.g. all
    # unverifiable), so there is no basis to call the page freshly
    # verified.
    next_last_verified = (
        previous_last_verified
        if exceeded_half or not checked
        else today.isoformat()
    )

    previous_confidence = fields.get("confidence", "").strip()
    next_confidence = previous_confidence
    if previous_confidence in CONFIDENCE_VALUES:
        if exceeded_half:
            next_confidence = _step_down_confidence(previous_confidence)
        if unverifiable:
            next_confidence = _cap_confidence_at_medium(next_confidence)

    return PageFactCheckPlan(
        rel_path=candidate.rel_path,
        claims=tuple(verdicts),
        checked_count=len(checked),
        failed_count=len(failed),
        unverifiable_count=len(unverifiable),
        exceeded_half=exceeded_half,
        previous_last_verified=previous_last_verified,
        next_last_verified=next_last_verified,
        previous_confidence=previous_confidence,
        next_confidence=next_confidence,
    )


def _page_sort_key(fields: dict[str, str], rel_path: str) -> tuple[int, str, str]:
    """Empty ``last_verified`` first, then oldest first (ТЗ 6.6). ISO dates
    sort correctly as plain strings; an invalid value is treated as empty
    (oldest priority) instead of silently sorting as a valid recent date.
    """
    last_verified = fields.get("last_verified", "").strip()
    if not DATE_ONLY_RE.match(last_verified):
        return (0, "", rel_path)
    return (1, last_verified, rel_path)


def select_pages_for_fact_check(
    service: CompiledBriefingService,
    *,
    limit: int,
) -> list[CompiledBriefingCandidate]:
    """ТЗ 6.6 page selection: core/active/warm tiers plus not-yet-scored
    (empty tier) pages, any domain (``compiled/archive/`` is already
    excluded by ``_iter_candidates``), ordered empty-then-oldest
    ``last_verified``, capped at ``limit``.
    """
    eligible = [
        candidate
        for candidate in service._iter_candidates()
        if not candidate.tier or candidate.tier in FACT_CHECK_ELIGIBLE_TIERS
    ]
    eligible.sort(
        key=lambda candidate: _page_sort_key(
            CompiledBriefingService._frontmatter_fields(candidate.text),
            candidate.rel_path,
        )
    )
    return eligible[:limit]


def _decision_queue_entry(plan: PageFactCheckPlan, *, today: date) -> dict[str, str]:
    return {
        "kind": _DECISION_KIND,
        "page": plan.rel_path,
        "summary": (
            f"проверка фактов: {plan.failed_count}/{plan.checked_count} "
            "проверяемых утверждений не подтвердились — решить, актуальна ли страница"
        ),
        "since": today.isoformat(),
    }


def _write_fact_check_journal(
    vault_path: Path,
    *,
    today: date,
    plans: list[PageFactCheckPlan],
    patched_pages: list[str],
    flagged_pages: list[str],
    errors: list[str],
    queue_evictions: int,
) -> None:
    """Persist this run's own record, in a file separate from the
    compile-enrich pass journal (``.session/compile-enrich.json``) on
    purpose: that file is overwritten whole by every enrichment pass, so
    sharing it would erase whichever process ran second.

    ``queue_evictions`` mirrors the field ``compiled_briefings.py``'s own
    pass journal carries (ТЗ 7.2: "факт вытеснения попадает в дайджест") --
    this run cannot fold into that pass's ``queue_evictions`` counter (there
    is no active ``CompileEnrichPass`` here, see ``run_monthly_fact_check``),
    so it gets its own record here instead, in this run's own journal.
    ``compiled_enrich_report._with_fact_check_evictions`` reads it back and
    adds it to the digest's count for the same ``date``, which is what
    actually closes ТЗ 7.2 for this pass; writing it here without that
    reader made the whole field an owner-invisible no-op.
    """
    payload = {
        "date": today.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pages_checked": [plan.rel_path for plan in plans],
        "pages_patched": patched_pages,
        "pages_flagged": flagged_pages,
        "errors": errors,
        "queue_evictions": queue_evictions,
    }
    _atomic_write_text(
        vault_path / _JOURNAL_RELATIVE_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def run_monthly_fact_check(
    vault_path: Path,
    *,
    page_limit: int = DEFAULT_FACT_CHECK_PAGE_LIMIT,
    today: date | None = None,
) -> dict[str, Any]:
    """ТЗ 6.6 entry point.

    Select a bounded batch of core/active/warm pages ordered by
    ``last_verified`` staleness, deterministically re-check every live
    claim on each (zero model calls), and patch only
    ``last_verified``/``confidence`` under one shared vault lock. A page
    whose checked claims fail more than half keeps its previous
    ``last_verified`` and is queued (``.session/decisions-queue.json``) for
    the owner's archival decision instead of being patched -- this
    function never archives a page itself.

    Independent from the compile-enrich pass in ``compiled_briefings.py``:
    its own page budget (``page_limit``, not ``MAX_PAGES_PER_PASS``), its
    own journal file, and no ``CompileEnrichPass``/snapshot machinery --
    that machinery exists only for passes that rewrite page bodies via a
    model (ТЗ 5.5); this pass never touches a page body.
    """
    resolved_today = today or date.today()
    resolved_vault_path = Path(vault_path).resolve()
    service = CompiledBriefingService(resolved_vault_path)
    selected = select_pages_for_fact_check(service, limit=page_limit)

    plans = [
        evaluate_page(resolved_vault_path, candidate, today=resolved_today)
        for candidate in selected
    ]

    patched_pages: list[str] = []
    flagged_pages: list[str] = []
    errors: list[str] = []
    queue_evictions = 0

    try:
        if plans:
            manifest = load_manifest_for_vault(resolved_vault_path)
            with vault_write_lock(resolved_vault_path) as lock:
                queue_entries: list[dict[str, str]] = []
                for plan in plans:
                    updates = plan.frontmatter_updates
                    if updates:
                        try:
                            patch_validated_vault_frontmatter(
                                resolved_vault_path,
                                resolved_vault_path / plan.rel_path,
                                updates,
                                manifest=manifest,
                                existing_lock=lock,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Monthly fact-check patch failed for %s: %s",
                                plan.rel_path,
                                exc,
                            )
                            errors.append(f"{plan.rel_path}: {exc}")
                        else:
                            patched_pages.append(plan.rel_path)
                    # Queuing must not depend on the write above succeeding: a
                    # page that failed its checks still needs an owner decision
                    # even if the frontmatter patch itself blew up.
                    if plan.exceeded_half:
                        flagged_pages.append(plan.rel_path)
                        queue_entries.append(
                            _decision_queue_entry(plan, today=resolved_today)
                        )
                if queue_entries:
                    # The owner must learn when this batch forced older queue
                    # entries out (ТЗ 7.2), same as every other producer of
                    # ``append_decision_queue_entries`` -- this call is in
                    # fact the prime candidate for it, since it is the only
                    # producer that can hand the writer more entries than
                    # ``QUEUE_CAP`` in one call (a ``--page-limit`` above 30,
                    # see ``run_compiled_monthly_verify.py`` and
                    # ``decisions_queue._trim_new_entries_to_cap``). There is
                    # no active ``CompileEnrichPass`` on this path to fold the
                    # count into (``_record_queue_eviction`` is a no-op
                    # without one), so it is returned to the caller and
                    # journaled below instead -- surfaced by the CLI's printed
                    # JSON and by this run's own journal rather than lost.
                    queue_evictions = append_decision_queue_entries(
                        resolved_vault_path, queue_entries, existing_lock=lock
                    )
    finally:
        # In ``finally`` for the same reason ``compiled_briefings.py``'s
        # ``run_nightly_maintenance`` journals its pass there (code review):
        # the frontmatter patches above are already on disk by the time
        # ``append_decision_queue_entries`` can blow up, and this journal is
        # the only place the digest learns what this run patched, what it
        # flagged, and how many queue entries it evicted. Skipping it on the
        # way out left those writes done but unreported.
        #
        # Swallowing its own failure is part of running here (code review):
        # an exception raised inside ``finally`` *replaces* whatever is
        # already propagating, leaving the real cause reachable only through
        # ``__context__``. So a full disk at the moment of the journal write
        # would have reported "диск полон" for a run that actually failed on
        # something else entirely -- and the log line below is the only place
        # the lost journal itself gets recorded.
        try:
            _write_fact_check_journal(
                resolved_vault_path,
                today=resolved_today,
                plans=plans,
                patched_pages=patched_pages,
                flagged_pages=flagged_pages,
                errors=errors,
                queue_evictions=queue_evictions,
            )
        except Exception as exc:  # noqa: BLE001 - must not mask the real cause
            logger.warning("Failed to write monthly fact-check journal: %s", exc)

    return {
        "status": "no-work" if not plans else "ok",
        "pages_checked": len(plans),
        "pages_patched": len(patched_pages),
        "pages_flagged": len(flagged_pages),
        "claims_checked": sum(plan.checked_count for plan in plans),
        "claims_failed": sum(plan.failed_count for plan in plans),
        "claims_unverifiable": sum(plan.unverifiable_count for plan in plans),
        "errors": errors,
        "queue_evictions": queue_evictions,
    }
