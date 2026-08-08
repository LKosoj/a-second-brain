# Memory, notes, and search

[Русский](../ru/memory-and-search.md) | [Documentation index](../index.md)

This page explains how A Second Brain stores notes, selects context, finds old
material, and keeps memory useful as the vault grows.

## The short version

The private vault's Markdown files, attachments, and imported originals are the
source of truth. Search indexes, compiled briefings, memory scores, and session
files help the assistant work with those sources, but do not replace them.

For each question, the assistant uses the smallest useful combination of:

1. Curated core context such as `MEMORY.md` and current goals.
2. Compiled briefings for fast status and history answers.
3. QMD semantic search for notes with related meaning.
4. Exact text search for literal values such as an ID, path, or date.

This avoids loading every note into every prompt. It also keeps old material
available without letting it dominate routine answers.

## What is stored where

| Path | Role | Source or derived |
|---|---|---|
| `vault/MEMORY.md` | Curated facts, preferences, and decisions that should remain useful for weeks or months | Source |
| `vault/daily/` | Raw chronological capture from Telegram and integrations | Source |
| `vault/goals/` | Three-year vision and yearly, monthly, and weekly focus | Source |
| `vault/thoughts/` | Reusable ideas, learnings, project notes, and reflections | Source |
| `vault/business/` | CRM, professional network, and event context | Source |
| `vault/projects/` | Clients, leads, and active projects | Source |
| `vault/imports/` | Archived documents, web pages, YouTube material, and PLAUD recordings | Source |
| `vault/compiled/` | Short operational briefings assembled from source notes | Derived |
| `vault/summaries/` | Periodic reports and reusable assistant outputs | Derived |
| `vault/MOC/` | Maps of Content: navigation notes that link a domain together | Source or maintained navigation |
| `vault/attachments/` | Images and other uploaded binary files | Source |
| `vault/.qmd/` | Project-local QMD configuration, database, model cache, and locks | Derived runtime state |
| `vault/.memory-entries.json` | Memory metadata for individual entries inside daily files | Derived runtime state |
| `vault/.compiled/`, `.graph/`, `.session/` | Briefing state, graph reports, and current session handoff | Derived runtime state |

`vault-manifest.json` defines the allowed user-content roots, infrastructure
paths, QMD index name, context budget, and required note metadata. The runtime
uses this manifest when validating writes.

Deleting derived state does not intentionally delete the Markdown sources, but
do not remove runtime directories casually. Follow the backup and restore
procedure if rebuilding an instance.

## How incoming material becomes knowledge

The first write preserves the input. Voice is transcribed, text is appended to
the current daily file, photos are saved under attachments, and documents are
saved on disk before extraction. Supported external sources are archived under
`imports/` with enough provenance to return to the original.

The full daily cycle then runs three phases in separate agent contexts:

1. **Capture** reads the day's entries and classifies them into structured JSON.
2. **Execute** creates the notes and context updates selected during
   classification, plus optional Todoist tasks.
3. **Reflect** writes the report, updates the rolling handoff, runs memory and
   vault-health maintenance, and refreshes QMD.

`/process` stops after a read-only preview. `/process_full` runs the write-heavy
cycle immediately. The installed timer runs the same full cycle at 21:00.

Not every daily entry needs a separate card. A durable card is useful when a
fact, decision, relationship, lesson, or project state should be found and
updated independently. Transient chronology can remain in `daily/`.

## How context is selected

### 1. Bounded core context

The assistant eagerly loads a small set of high-value files:

- `MEMORY.md`;
- weekly, monthly, and latest yearly goals;
- today's and yesterday's daily notes;
- business and project indexes;
- the previous session handoff;
- bounded vault, Git, graph, and hygiene summaries.

The default total budget is `200000` bytes and is set in
`vault-manifest.json`. If the pack is too large, lower-priority sections are
replaced by explicit omission markers. `MEMORY.md` and weekly goals are kept.
The assistant therefore knows the current focus without reading the full
archive.

Vault text is treated as evidence, not as executable instructions. A command
written inside a note does not gain authority merely because it was retrieved.

### 2. Compiled briefings

Compiled briefings are concise operational summaries across projects, people,
meetings, topics, and decisions. They are useful for questions such as “What is
the current status?” or “What changed since the last review?”

Compiled pages live under `compiled/<domain>/`, one page per project, person,
topic, decision, meeting, or concept — six domains in total. `concepts` holds
durable, portable ideas, methods, or approaches; a candidate that is really
about one project or person — because its title carries a date, or fully
names an existing project or people page — is routed to `topics` instead,
since a dated or project-bound note is a snapshot, not a portable concept.

Every compiled page ends with an `## Owner Notes` section that wraps a human
zone marked by `<!-- human:start -->` / `<!-- human:end -->` comments.
Anything written inside those markers belongs to the owner, not the pipeline,
and survives every recompilation, compression, archival, and link-repair pass
untouched.

Beyond the frontmatter fields other notes use, a compiled page also carries
fields that only code writes, never the model: `sources_trust`,
`last_verified`, `enrichment_count`, `conflicts_open`, and `human_reviewed`.
`last_verified` records when the page's claims were last confirmed — by a
fresh claim landing, or by the fact-check pass described below. That is a
different signal from the ordinary `last_accessed` field, which only tracks
when the page was last opened: a frequently opened page is not automatically
a frequently checked one.

Every source that lands on a page is scored into one of four trust levels,
from weakest to strongest: `inferred`, `forwarded`, `integration`, `own`. A
page's `sources_trust` is the weakest level among its sources. Only `own` and
`integration` are strong enough, alone, to justify an automatic action with
real consequences, such as creating a task or superseding an existing claim;
`forwarded` and `inferred` alone never are.

A new claim that contradicts an existing one is classified as `temporal`,
`factual`, or `contextual`. A temporal conflict — an opinion that changed
over time, or two claims with different known source dates — resolves
itself: the newer source wins. A factual conflict does not resolve on its
own: both claims stay on the page, and the disagreement becomes one row the
owner must resolve through the decisions queue described below. A
contextual conflict resolves itself too: both claims are valid in their own
scope, so the new claim is simply recorded on the page, with the model's
explanation of how the scopes differ, when it gave one, appended to that
claim's own row. Code forces the `temporal` type for an opinion-kind claim
or when both source dates are known and differ; otherwise it accepts the
model's own label as given, and only falls back to `factual` when that label
is missing or invalid.

See `skills/compile-enrich/SKILL.md` for the full domain table, trust and
conflict rules, and page schema.

Each briefing links back to its sources and records freshness and confidence.
It is still derived data. When an answer depends on a precise fact, the runtime
can verify the briefing against curated context or the source note.

Relevant source writes enqueue a briefing refresh. Post-cycle maintenance drains
that queue, checks required sections and source links, refreshes stale
briefings, archives obsolete ones, and updates QMD when searchable content
changed. Each background queue run writes a separate journal under
`.compiled/queue-history/` with its source events, outcomes, changed pages, and
errors. The latest 10 journals are retained.

A reusable direct answer can also be filed under `summaries/answers/`. The
current filter rejects outputs shorter than 180 characters. Longer outputs are
filed only when they contain a list, reach 350 characters, or the request
contains one of the current cues: `что`, `status`, `статус`, `решили`,
`изменил`, or `summary`. This prevents every short reply from becoming another
note.

### 3. QMD semantic retrieval

Semantic search means search by meaning rather than only by matching words.
QMD turns note chunks into embeddings, which are numeric representations used
to compare meaning. A reranker is a second model that reorders the best
candidates for the specific query.

The project keeps its QMD index under `vault/.qmd` and does not use the user's
global QMD state. It indexes core notes, daily entries, thoughts, compiled
briefings, imported sources, goals, business, projects, summaries, and MOCs.

With empty `EMB_MODEL` and `RERANK_MODEL` settings, QMD uses local
`Qwen3-Embedding-0.6B` and `Qwen3-Reranker-0.6B` models on CPU. Remote endpoints
are optional. See [Integrations](integrations.md#qmd) for installation and
model settings.

### 4. Exact lookup

Semantic search is the wrong tool for an exact environment variable, order ID,
filesystem path, code symbol, or literal phrase. For those questions, the agent
or operator uses exact text search such as `rg` against the relevant vault
source or derived directory. There is no separate `a-second-brain` exact-search
subcommand. Exact search does not silently replace a failed semantic search,
because the two searches answer different questions.

### 5. Links and Maps of Content

Wiki-links and MOCs provide stable human navigation. The graph tools can find
broken links, isolated notes, and weak metadata. New links should express a
real relationship, not just a shared word. A few strong links are more useful
than many speculative ones.

The current domain hubs are:

| Domain | Main paths | Hub |
|---|---|---|
| Personal | `thoughts/`, `goals/`, `daily/` | `MEMORY.md` |
| Business | `business/*.md` | `business/_index.md` |
| Projects | `projects/*.md` | `projects/_index.md` |

## Memory metadata

Markdown notes can start with frontmatter, a YAML metadata block between `---`
lines. A durable knowledge card normally includes:

```yaml
---
type: project
description: "One useful sentence shown in search results"
tags: [client-work, launch]
status: active
created: 2026-08-04
updated: 2026-08-04
last_accessed: 2026-08-04
relevance: 1.0
tier: active
---
```

`status` describes the real-world lifecycle of the item, such as an active or
completed project. `tier` describes only its memory visibility. They are not
interchangeable.

The default memory configuration is in `vault/.memory-config.json`:

```json
{
  "tiers": {"active": 7, "warm": 21, "cold": 60},
  "decay_rate": 0.015,
  "relevance_floor": 0.1
}
```

Relevance follows a floor-adjusted exponential curve:

```text
relevance = floor + (1 - floor) * exp(-decay_rate * days_since_access)
```

The default tiers are:

| Tier | Effective memory age | Normal recall |
|---|---:|---|
| `core` | Not automatically demoted | Always eligible and strongly preferred |
| `active` | 0–7 days | Eligible |
| `warm` | 8–21 days | Eligible |
| `cold` | 22–60 days | Hidden unless the semantic match is exceptionally strong |
| `archive` | More than 60 days | Hidden unless the semantic match is exceptionally strong |

`deep-recall` includes every tier. It keeps the same memory, recency, and
supersession score adjustments; it only removes the normal tier-visibility
filter. Decay changes metadata and ranking but never deletes the note.

## Recall ranking

`a-second-brain qmd recall` asks QMD for semantic candidates, then applies
memory-aware ranking:

- core, active, and warm tiers receive progressively smaller boosts;
- cold and archive receive small penalties;
- relevance contributes a smaller positive adjustment;
- recent dated notes receive a recency boost, while old dated notes can be
  penalized;
- evergreen paths such as `MEMORY.md`, goals, MOCs, and the business and project
  indexes are not penalized for age;
- a superseded fact receives a strong penalty but remains traceable.

Normal recall allows core, active, warm, and unclassified notes. A cold or
archived note can still pass when its base semantic score is at least `0.9`.
The final list is sorted by effective score and exposes a confidence value
clamped to the `0`–`1` range.

These adjustments affect discovery order, not truth. Open the selected source
before relying on its exact wording.

## Meaningful access and promotion

Using a note reinforces it. `a-second-brain qmd get` records access
automatically. If an operator or agent reads a Markdown file directly, it
should run the memory engine's `touch` command afterward.

A touch promotes only one tier:

```text
archive -> cold -> warm -> active -> active refresh
```

For a one-tier promotion, the engine moves `last_accessed` to a representative
date inside the target tier instead of always setting it to today. This
prevents one accidental lookup from making an old note immediately dominant.
Core notes stay core. Bulk maintenance must not touch every file, because that
would destroy the access signal.

## Daily entries age independently

One daily Markdown file can contain a morning decision and an evening reminder.
They should not necessarily have the same memory age. The runtime therefore
tracks sections whose headings follow this form:

```markdown
## 09:15 [voice]
...

## 18:40 [text]
...
```

Per-entry metadata is stored in `vault/.memory-entries.json`. QMD uses the
hottest entry as the daily file's memory signal. Touching the daily file
promotes all tracked entries in that file. The JSON store is derived private
state, not a second source of note content.

## Correcting durable facts

Important facts can carry epistemic metadata: metadata about how the fact is
known.

| Field | Meaning |
|---|---|
| `epistemic_confidence` | `verified`, `inferred`, or `unverified` |
| `epistemic_scope` | The person, project, period, or situation to which the fact applies |
| `epistemic_state` | `active` or `superseded` |
| `epistemic_verification` | Required evidence description for a verified fact |

Do not silently overwrite an important old fact when the correction itself is
useful history. Create a successor note and use `supersede`. The command marks
the old note and writes reciprocal `superseded_by` and `supersedes` links. Search
then penalizes the old version while preserving the chain.

## Compiled-page aging and re-verification

Compiled pages age through the same memory tiers used elsewhere in this
document. The tier controls queue priority, verification depth, compression,
and search visibility, but it does not suppress a relevant new source:

- `core` and `active` pages are fully compiled and checked whenever a
  relevant source lands.
- `warm` and `cold` pages are compiled when a relevant new source lands, so
  the derived page does not remain stale. Verify samples their new claims.
- `archive` pages that receive a relevant new source are promoted to `warm`
  and enriched in the same pass.

When a page cools into `warm`, `cold`, or `archive`, its `Recent Changes` and
`Open Loops` sections are compressed: only the most recent entries stay in
place, and open loops left open long enough move into a `History` section
marked abandoned. The sources table (“Sources That Shaped This Page”), the
claim history, open conflicts, and the human zone are never compressed by
this pass — they only grow, or shrink through an explicit owner decision in
the decisions queue.

A page that has stayed at the `archive` tier long enough, with no incoming
links, is moved into `compiled/archive/<domain>/` instead of being deleted.
Nothing under `compiled/` is ever deleted by this pipeline.

Fact-checking itself runs every night, not on a monthly schedule — but each
run only processes a small, bounded batch of the stalest `core`/`active`/
`warm` pages, so with a normal vault size any single page is actually
rechecked roughly once a month. It calls no model: it re-derives each
claim's category from its recorded source and re-checks the wikilinks it
references. A page whose checked claims mostly still hold has its
`last_verified` date moved to today. A page that fails instead keeps its
previous `last_verified` date, has its `confidence` stepped down one level,
and is queued for the owner as a `fact-check-rejected` decision instead of
being silently patched or archived on its own; a page with even one
unverifiable claim also has its `confidence` capped at `medium` either way.

## Owner tools for the compiled layer

The dashboard (`/menu`) adds four buttons on top of the compiled layer:
**📰 Дайджест** (digest), **🗂 Очередь** (queue), **📝 Бриф** (brief), and
**📅 Сводка недели** (weekly review).

**Daily digest.** The digest button, and the same step in nightly
maintenance, builds a short report: what needs a decision, what changed
today, and one or two long-forgotten pages worth another look. A "needs a
decision" item that first appeared today is shown in full; an item that was
already on the queue is folded into one summary line with a count and a
pointer to the "Очередь" screen, so an old conflict is never restated in
full every day, but it never silently drops out of view either. It is
written to `summaries/compile/YYYY-MM-DD.md` and, during the nightly cycle,
sent to the owner as its own Telegram message. The digest stays silent only
on a genuinely quiet day: the pass took no work, reported no error, the
merged decisions list — the internal queue file's entries plus every open
page conflict — is empty, no compiled page changed today, no pass budget
was exhausted, no queue entries were evicted by the 30-item cap, no page's
human-owned zone markers were left ambiguous, the background queue worker
did not crash, and the refresh queue did not permanently run out of attempts
on any source. So a single open conflict still forces a digest, even when
the pass otherwise did nothing — and so do the other six cases, each for its
own reason: pages are also enriched during the day by the background queue
drain, so a night that honestly finds nothing left to do can still sit on
top of real changes; silent truncation from an exhausted budget is
explicitly forbidden; an evicted queue entry is a fact the owner must learn;
a page stuck on an ambiguous human-zone marker cannot resolve itself on a
later pass the way the other cases eventually can; a crashed background
worker means no new write reaches a compiled page at all until it is
restarted (the reason comes from `.session/compile-queue-worker.json`); and
a source the queue gave up on will never re-enter it on its own — until the owner opens and saves the note again, no compiled
page for it will exist at all. That give-up list lives in
`.session/compile-dropped-sources.json`, is not keyed by date (so it repeats
in the digest every day until the source finally compiles), and is rendered
as the first five names plus a count of the rest, so a backend outage across
a hundred notes cannot bury the digest under itself.

**Decisions queue.** The queue button opens a paginated list, 8 items per
screen; there is no overall limit on how many items the list can hold. The
list is available in two places at once: the interactive `/menu` screen and
a human-readable file, `summaries/compile/decisions-queue.md`, regenerated
on every queue change. Both are built from the same data — a small internal
file (`.session/decisions-queue.json`) plus every compiled page's own “Open
Conflicts” table — so they cannot drift apart. Eight kinds of items exist
today; four have their own dedicated actions:

- a page that failed its fact-check offers “confirm the page is current,”
  “archive the page,” or “defer”;
- an open conflict shows the two competing claims themselves as button
  labels, so the owner picks between the actual wording, plus “defer”;
- a page whose Verify step rejected most of its proposed claims several
  times in a row against the same source snapshot offers “retry the
  check” — this clears the page's rejection counter so the next pass gives
  Verify another attempt instead of skipping the page as still exhausted —
  or “stop retrying,” which just drops the queue entry and leaves the
  page's counter as it was, plus “defer”;
- a possible duplicate — a new page that turned out too close to an
  existing page in the same domain — offers “link as similar” or “mark as
  distinct,” plus “defer.” The two pages are never merged: “link” only sets
  a mutual `duplicate_of` frontmatter marker on both pages; any actual
  merge stays the owner's call.

The other four — a page that outgrew its monthly enrichment budget
(“drift”), a claim replacement blocked by a low-trust source
(“blocked-action”), a page file saved in an unreadable encoding
(“page-encoding-broken”), and a page whose `<!-- human:start -->` /
`<!-- human:end -->` markers do not resolve to a single span
(“human-zone-ambiguous”) — plus any other, truly unrecognized kind found in
the queue file all fall back to just “reject” and “defer”; nothing here
guesses what an unfamiliar kind should do. For the last two the remedy is
outside the queue entirely: re-save the file as UTF-8, or fix the markers by
hand — until then the page is neither enriched nor compressed. Only the small
internal queue file has a size limit, 30 entries: past that, the oldest
entries are dropped first, except entries whose *kind* is `conflict` or
`blocked-action`. Open conflicts, however, never actually land in that
file — they are read live from each page's own table and are not capped or
evicted at all — so today that protection has nothing to protect, and it is
not based on the page's memory tier; the code has no such check.

**Weekly review.** The "Сводка недели" button opens the half-hour weekly
ritual screen: the 7-day window ending today; the week's changes; a
5-item preview of the decisions queue with a count of what is left and a
pointer to the full "Очередь" screen; one long-forgotten page related to the
week's changes (the same selection rule the digest uses, but at most one
here); and one page from this week's changes suggested for a "confirm
reviewed" tap, which stamps its `human_reviewed` field with today's date.
Both the forgotten-page and the confirm-review pick are deterministic for a
given day but are not pinned to the same page forever. There is no separate
schedule behind this screen — it is rebuilt from already-compiled data every
time it is opened.

**`/why`.** `/why <query>` answers “why does this page say that” for one
compiled page: its sources by date, trust level, claim-history replacements,
open conflicts, when it was last fully verified, and whether/when the owner
confirmed it. When the candidate pages are close enough that guessing would
be unsafe, `/why` asks the owner to pick between them instead of guessing —
two of them on a close lexical near-tie, and one per domain (up to six) when
the same slug exists in several of them.

**Provenance on answers.** A direct-question answer that drew on compiled
pages is followed by an “Источники ответа” (answer sources) block naming
those pages with their trust level and open-conflict count, plus a warning
when the combined trust is weak or a shown page carries an open conflict.

**Briefs.** The brief button offers three kinds — **Решение** (decision),
**Тема** (topic), **Проект** (project) — then asks for a free-text query. A
brief is assembled purely from an already-compiled page: it never triggers a
new compile pass, so a topic with nothing compiled yet has no brief to
offer. The result is filed under `summaries/briefs/`.

## Search and memory commands

Run these from the project or wheel-created instance directory:

```bash
# Search by meaning, preferring current memory
a-second-brain qmd recall "launch decisions"

# Include cold and archived material
a-second-brain qmd deep-recall "old launch experiments"

# Return structured results or change the result limit
a-second-brain qmd recall --json --limit 10 "client promises"

# Read a selected source and record access
a-second-brain qmd get projects/projects.md

# Refresh text inventory, build embeddings, and inspect status
a-second-brain qmd update
a-second-brain qmd embed
a-second-brain qmd status
```

Memory maintenance commands live in the public `agent-memory` skill:

```bash
uv run skills/agent-memory/scripts/memory-engine.py stats vault
uv run skills/agent-memory/scripts/memory-engine.py decay vault
uv run skills/agent-memory/scripts/memory-engine.py daily vault
uv run skills/agent-memory/scripts/memory-engine.py touch vault/thoughts/example.md
uv run skills/agent-memory/scripts/memory-engine.py creative 5 vault
uv run skills/agent-memory/scripts/memory-engine.py supersede thoughts/old.md thoughts/new.md --vault vault
```

`creative` samples warm, cold, and archived cards for idea generation. It is
not the routine retrieval path.

## Rules for creating and updating notes

- Search for a possible duplicate before creating a durable card.
- Open the source result before quoting it or changing a related note.
- Keep one durable fact in one primary place; connect other contexts with
  wiki-links.
- Write a useful `description`, sparse lowercase tags, and the real domain
  `status`.
- Add only links that express a meaningful relationship.
- Preserve source URLs and imported originals where the integration supports
  them.
- Put durable context in `MEMORY.md` only when it should remain useful for weeks
  or months. Put implementation progress and one-off events in daily notes or
  the rolling handoff.

## What runs automatically

| Mechanism | Automatic condition |
|---|---|
| Capture persistence | On each accepted Telegram message |
| Direct question retrieval | When the text classifier returns `high` confidence for the question route |
| Full daily processing | At 21:00 in the host's local time when the supplied timer is enabled, or manually with `/process_full` |
| Memory decay and creative recall | During the full reflection phase |
| Vault-health and compiled-briefing maintenance | During the full reflection phase and scheduled post-cycle maintenance |
| Compiled fact-check | Nightly, during scheduled post-cycle maintenance, after compiled maintenance and vault health, on a bounded batch of the stalest pages |
| Compiled digest delivery | Nightly, during scheduled post-cycle maintenance, after compiled maintenance, vault health, and fact-check; skipped on a quiet night |
| QMD refresh after searchable writes | Best effort; the separate QMD timer can also refresh the index |
| Weekly, monthly, and yearly reviews | During the scheduled cycle when the relevant boundary is due |
| PLAUD import | Only when configured and its timer or manual sync is used |
| Encrypted vault snapshot | Before scheduled processing only when backup settings are configured |

Automatic means the relevant service and timer are installed, enabled, and
healthy. A source checkout does not enable user services until
`./scripts/install-systemd-user.sh --enable` succeeds.

The calendar checks run on the date when the scheduled cycle actually starts:
the weekly digest on Friday, system reflection and goal rollover on Sunday,
monthly review on the last day of the month, and yearly review on December 31.
An overdue weekly goals file can trigger rollover on a later day. The other
periodic reviews do not have a general missed-run catch-up mechanism.

“Best effort” QMD refresh means a failed index update does not roll back the
source note. The error is logged, and a later write or the separate QMD timer
can retry the refresh.

## Limits and failure behavior

- Without QMD, capture and ordinary vault operation still work, but semantic
  retrieval and memory-aware recall are unavailable.
- Empty QMD model variables do not disable search; they select the local CPU
  models. Remote embedding and reranking require explicit settings.
- If semantic retrieval fails, the assistant should report that evidence gap
  instead of presenting an exact-text scan as an equivalent search.
- Compiled briefings and scores can become stale. The source note remains the
  authority.
- Memory tiers measure recent use, not importance or truth.
- Graph repair cannot infer every relationship reliably. Review proposed links
  when meaning is ambiguous.
- Self-hosting keeps the vault under your filesystem control, but configured
  providers may receive the data described in the [privacy boundary](../../README.md#privacy-boundary).

Back up the entire private vault, including hidden runtime state, by following
[Backup and restore](backup-and-restore.md). For search failures, see
[Troubleshooting](troubleshooting.md).
