---
type: note
description: Trust levels, claim kinds, conflict-type resolution, Verify sampling, and pass budgets for the compile-enrich pipeline. Mirrors src/d_brain/services/compiled_briefings.py; do not restate it as an independent spec.
last_accessed: 2026-08-05
relevance: 0.6
tier: warm
---

# Conflict And Trust Policy

## Source Trust

Four levels, `SOURCES_TRUST_VALUES`: `own > integration > forwarded >
inferred` (`TRUST_RANK`: own=4, integration=3, forwarded=2, inferred=1).
Trust is decided **by code, never by the model** (`_source_trust_level`).
It does not pick a conflict winner and it does not veto one either -- it is
handed to the conflict adjudicator as evidence about where the words came
from (see below). The rule depends on where the source lives, not just its
entry marker:

- `thoughts/` -- always `own`.
- `imports/plaud/` (dictaphone meeting recordings) -- always capped at
  `forwarded`, regardless of any marker.
- `imports/` (other integrations) -- always `integration`.
- `daily/` -- read off the excerpt's own entry headers (`[voice]`,
  `[text]`, `[photo]` = own; `[forward from: ...]` = forwarded; any other
  marker = inferred); an excerpt spanning more than one entry takes the
  weakest level across all of them.
- anything else -- `inferred` (fails closed).

Only `own` and `integration` are strong enough, alone, to justify an
automatic action with consequences outside the compiled layer -- creating a
task, editing CRM data (`_trust_allows_consequential_action`, used by
`processor.py` and the bot capture handlers). `forwarded`/`inferred` alone
must never trigger one.

Superseding a claim on a compiled page used to be on that list and is not
any more. The veto cost more than it bought: a PLAUD recording is capped at
`forwarded` because it has no speaker diarization, so every date-based
supersession coming out of a meeting recording was downgraded to a factual
conflict and turned into an owner task -- the queue filled with pairs no
rule could settle. Reading the transcript is exactly what tells the two
apart, so that call now goes to a model that can read it, with the trust
level stated as one of the things to weigh.

## Claim Kinds

`CLAIM_KIND_VALUES`: `fact | opinion | commitment`.

## Conflict Adjudication

Every conflict is put to a model, one call per pair
(`_adjudicate_conflict`). The call is told both claims, both source dates,
the new source's trust level in plain language, the claim kind, the page's
Current State, and what the compile stage labelled the conflict -- all as
evidence, none of it as a rule. It answers with one of four outcomes
(`CONFLICT_OUTCOME_VALUES`), which map onto the three conflict types the
page rendering already knew how to execute (`CONFLICT_OUTCOME_TO_TYPE`):

| Outcome | What the page does |
|---|---|
| `new_supersedes` | the existing claim leaves "Sources That Shaped This Page" for "Claim History", crediting the new source |
| `existing_stands` | the new claim is never added; the existing claim stays current |
| `both_valid` | both stay, each in its own scope, and the model's `context_note` is appended to the new claim's own row |
| `unclear` | both stay and the pair is written to "Open Conflicts" |

Guidance carried in the prompt, not applied to the answer afterwards: dates
are a strong argument for the newer version but not a law (a newer entry can
be a retelling of an older one, or someone else's line); trust says where the
words came from, not whether they are true; the owner's opinions legitimately
change over time.

Every failure -- unreachable model, unparseable JSON, an outcome outside the
four -- returns `unclear`. That is the same both-claims-kept landing spot the
whole path used to fail closed into, so a flaky network can never make a page
assert one side. The one exception allowed through is
`CompiledBriefingPassBudgetExceededError`: that is not a failure to decide,
it ends the pass's work on the source and leaves it queued.

## Retrying What Could Not Be Settled

`unclear` also queues an `undecided-conflict` entry -- a retry buffer, not a
task for the owner. The nightly pass re-adjudicates every still-open conflict
on every compiled page (`_resolve_open_conflicts`, bounded by
`MAX_CONFLICT_RETRIES_PER_PASS` = 20 conflicts per pass) with `attempt=2`:
the prompt says outright that this pair has been seen before and that a
decision is required this time, and the `unclear` option is not offered.
Still undecided is not an escalation -- the row simply stays and the next
pass asks again. Once a page has no open conflict left, its
`undecided-conflict` entry (and any legacy `blocked-action` entry) is removed
from the queue.

A page that carries open conflicts but has no "Claim History" section cannot
retire a claim: `_replace_section` is a no-op on a missing heading, so going
ahead would remove the claim from the live ledger with nowhere to put it.
Since every rendered page has that section, a page without one was edited by
hand -- so `_ensure_claim_history_section` puts an empty one back in its
canonical slot (before `## History` if present, otherwise before
`## Owner Notes`) and the retry proceeds. Only when there is no such anchor
either, or when the human zone's markers are ambiguous, is the page skipped
with a warning -- before any model call is spent on it.

The owner can still answer a conflict by hand from the "Очередь" screen; that
path takes exactly the same action (`decisions_queue._retire_losing_claim`),
so a tap and a verdict leave the page in the same shape.

## Verify

One batched, read-only, clean-context model call per page, checking whether
each sampled claim actually follows from the source excerpt given (no
network access, so it can only judge entailment, not look anything up).
The same call must explicitly pass three page-wide checks before a write:
every substantive statement has source coverage, all content belongs to the
target topic, and dates/statuses/owners/outcomes are internally consistent or
retain the source's uncertainty. A missing or failed check rejects the page.
Sampling rate: 100% of new claims for `core`/`active` tier pages, 25%
(minimum 1) for everything else -- `warm`, `cold`, `archive`, or an
unset/unknown tier all fall back to the warm fraction (fail-closed rather
than skipping Verify for a tier the source ТЗ never named).

A rejected claim never reaches the page. If Verify rejects a majority of one
page's newly proposed claims, the page is still written -- with
`quality_status: needs_review` and the reason in `quality_reason` -- rather
than skipped entirely, and `last_verified` is left where it was.

A Verify reply broken *as a format* (unparseable JSON, or a missing/mistyped
`page_checks` or `page_issues`) is treated differently from a rejection: the
model said nothing about these claims, so they are kept unverified and the
page carries the same `needs_review` mark with the reason
(`_mark_verify_unavailable`). Every such reply also counts toward the pass
journal's `verify_format_drift`.

Because both paths now write a page, a page no longer reaches the decisions
queue as a `verify-rejected` item in practice; `_queue_verify_rejected` and
`MAX_VERIFY_REJECTED_RETRIES` stay in place only for the escalation path that
has no candidate payload to carry the mark. See "Decisions Queue Item Kinds"
below.

## Pass Budgets (`run_nightly_maintenance`)

- `MAX_PAGES_PER_PASS` = 40 -- distinct compiled pages one pass may write.
- `MAX_MODEL_CALLS_PER_PASS` = 200 -- every impact/compile/verify/JSON-repair
  call shares this one budget.
- `MAX_ENRICHMENTS_PER_PAGE_PER_MONTH` = 20 -- beyond this, further source
  material for that page waits until the month rolls over, and a `drift`
  entry is queued for judgement. Counted as distinct (date, source) pairs in
  "Sources That Shaped This Page", not as rows: one enrichment appends one
  row per claim, so counting rows charged a single pass several times over
  and froze pages enriched two or three times.
- `MAX_CLAIMS_PER_PASS` = 20 -- claims accepted from one model response.
- `MAX_CONFLICT_RETRIES_PER_PASS` = 20 -- still-open conflicts one pass
  re-adjudicates (one model call each).
- `MAX_DRIFT_JUDGEMENTS_PER_PASS` = 5 -- queued drift suspicions one pass
  judges (one model call each).

Exhausting a budget ends the pass normally: the remainder stays queued, and
the exhaustion itself is reported in the owner's daily digest -- it is never
treated as a failure.

## Monthly Fact-Check (`compiled_fact_check.py`)

A separate, narrower, fully deterministic pass (**zero model calls**) from
the nightly compile-enrich pass above. It only ever patches two frontmatter
fields, `last_verified` and `confidence`. A page that fails its check is
left unadvanced and queued as a `fact-check-rejected` decision rather than
being silently patched or archived.

"Monthly" is the per-page cadence, not the schedule: the pass itself runs
every night, in the same scheduled cycle as everything else here
(`maintenance.compiled-fact-check`, `triggers=("scheduled-post",)`). Each
run takes only the stalest `DEFAULT_FACT_CHECK_PAGE_LIMIT` = 20
core/active/warm pages, which works out to revisiting any single page
roughly once a month. There is no separate monthly timer.

## Drift Judgement

Hitting `MAX_ENRICHMENTS_PER_PAGE_PER_MONTH` is a *suspicion* of drift, not
a finding: a busy project page hits the cap exactly the way a page losing
its shape does, and the counter cannot tell them apart. The nightly pass
asks instead (`_adjudicate_drift_entries`): the model is shown the page's
Current State and every row added to it that month, and answers whether the
page actually drifted. A confirmed drift is recorded on the page as
`quality_status: needs_review` with the reason -- the same two fields Verify
uses, cleared by the next clean Verify pass -- and either way the queue entry
goes, because the question has been answered. A failed or unusable call
leaves the entry for the next pass rather than answering "no drift" with
silence. The enrichment cap itself is untouched by the verdict: it is a cost
control, not a drift finding.

## Decisions Queue Item Kinds

Nine kinds exist today (`services/decisions_queue.py`). Two of them --
`conflict` and `drift` -- are drained automatically by the nightly pass and
reach the owner only in the window between the pass that could not settle
them and the pass that does:

- `fact-check-rejected` -- written into `.session/decisions-queue.json` by
  `compiled_fact_check.py`. A response either confirms the page (bumps
  `last_verified`) or rejects it (archives the page); either way the queue
  entry is removed.
- `conflict` -- not stored in the JSON file at all. It is derived live, one
  item per open-conflict row, by reading every `compiled/**` page's own
  "Open Conflicts" table directly. A response picks the side the page
  asserts: the row goes, `conflicts_open` drops, and the losing claim moves
  to "Claim History" with the source that displaced it.
- `undecided-conflict` -- written into `.session/decisions-queue.json` by
  `CompiledBriefingService._queue_undecided_conflict` when the adjudicator
  answered `unclear`. A retry buffer for the nightly pass, not an owner
  task; deduped by (kind, page), removed once that page has no open conflict
  left. Protected from eviction under `QUEUE_CAP`, same as `conflict`. No
  dedicated response -- the actual choice happens through the page's own
  `conflict` item above.
- `blocked-action` -- **retired producer.** It was written when a
  `forwarded`/`inferred` source would otherwise have won a temporal
  supersession by date alone and trust vetoed applying it. Trust no longer
  vetoes anything (see "Source Trust" above), so nothing produces this kind;
  the constant stays because entries written before the change are still on
  disk, and the nightly conflict retry clears them alongside
  `undecided-conflict`.
- `duplicate-candidate` -- written into `.session/decisions-queue.json` when
  Resolve (ТЗ 5.2) creates a new page whose confidence against an existing
  same-domain page falls in the possible-duplicate zone (0.85-0.95, see
  `RESOLVE_POSSIBLE_DUPLICATE_CONFIDENCE_THRESHOLD` /
  `RESOLVE_SAME_PAGE_CONFIDENCE_THRESHOLD` in `compiled_briefings.py`). A
  response either "link"s the two pages (sets a mutual `duplicate_of`
  frontmatter pointer on both, patched independently) or "distinct"s them
  (clears the queue entry, touches neither page) -- this module never
  merges pages itself; a real merge is always the owner's call.
- `drift` -- written into `.session/decisions-queue.json` by
  `CompiledBriefingService._queue_monthly_drift` when a page's monthly
  enrichment budget (`MAX_ENRICHMENTS_PER_PAGE_PER_MONTH`) is reached. No
  dedicated owner response exists for it -- only the generic
  `reject`/`defer` below -- because the nightly pass judges it instead; see
  "Drift Judgement" above.
- `verify-rejected` -- written by
  `CompiledBriefingService._queue_verify_rejected` once a page's Verify step
  has rejected a majority of its proposed claims
  `MAX_VERIFY_REJECTED_RETRIES` times in a row against the same source
  snapshot (both the incremental refresh path and the nightly freshness
  backfill count toward the same retry counter). Incremental entries retain
  the source event, including for a page that has not been created yet.
  "Повторить проверку" clears the page's retry counter and returns that
  source to the refresh queue; the generic `reject` below just drops the
  queue entry and leaves the page exhausted as-is. No producer reaches this
  in practice any more -- see the Verify section above: a page that fails
  Verify is written with `quality_status: needs_review` instead of being
  escalated here.
- `page-encoding-broken` -- written by
  `CompiledBriefingService._queue_undecodable_page` when a compiled page's
  bytes on disk are not valid UTF-8. Every writer refuses such a page rather
  than rewrite it and commit U+FFFD over the owner's real bytes, so it stops
  being updated and compressed until the owner re-saves the file as UTF-8.
  No dedicated response -- the remedy is outside the vault entirely.
- `human-zone-ambiguous` -- written by
  `CompiledBriefingService._queue_human_zone_ambiguous` when a page's
  `<!-- human:start -->` / `<!-- human:end -->` markers do not resolve to
  exactly one span. Every writer fails closed rather than guess which text
  is the owner's, and the background drain then releases the refresh event
  for a free retry every 300s indefinitely, so the queue entry is the only
  thing that tells the owner to go fix the markers. No dedicated response --
  the remedy is editing the markers by hand.

Both of the last two are written to the queue rather than only to the pass
journal on purpose: the journal exists only inside `run_nightly_maintenance`,
so on the background drain -- the far more common producer of both -- a
journal-only signal is a silent no-op.

Any other `kind` found in the JSON file is treated honestly as unsupported:
only `reject` (drop it) and `defer` (leave it) are offered for it.

## Human-Readable Queue Mirror

`decisions_queue.write_queue_document` renders the same queue items the
bot's "Очередь" screen shows to `summaries/compile/decisions-queue.md`,
regenerated on every mutating response and, as a safety net, once more
nightly from `maintenance.compiled-digest`. Both surfaces read the same
data, so they cannot disagree about what is open.
