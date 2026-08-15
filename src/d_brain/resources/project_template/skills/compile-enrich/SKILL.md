---
type: note
description: Routing doc for the compile-enrich pipeline that maintains compiled/ briefings above raw vault notes -- the six-domain classification, trust/conflict rules, and the owner-facing tools (/why, briefs, digest, decisions queue) that read those pages.
last_accessed: 2026-08-05
relevance: 0.8
tier: active
name: compile-enrich
depends_on: [graph-builder, vault-health]
---

# compile-enrich

Routing doc for the compile-enrich pass and the derived `compiled/` layer it
maintains above raw/searchable vault notes.

## What This File Is

A routing and source-map document, in the same spirit as
`dbrain-processor/SKILL.md`. Use it to find:
- where the compile-enrich pass and its budgets/trust rules actually live
- which satellite modules read the compiled layer for the owner
- how pages are classified, verified, and archived
- where the CLI and bot surface for this pipeline live

## What This File Is Not

Stricter than `dbrain-processor/SKILL.md`: **not one file under this skill
directory is ever loaded by the runtime.** There is no `phases/` directory
here that gets injected into a live prompt the way `dbrain-processor`'s
phase files do -- the compile-enrich prompts, budgets, trust rules, and page
rendering are all built inline in Python, primarily in
`src/d_brain/services/compiled_briefings.py`. This file and its three
`references/` files exist only so a human or an agent can find that code
quickly; they must never become a second, competing spec. If this file and
the code ever disagree, the code is right -- fix this file, not the other
way around.

## Editing Protocol

1. Change behavior in code first: `compiled_briefings.py`, one of the
   satellite modules below, a `run_compiled_*.py` CLI, or a
   `bot/handlers/*.py` file.
2. Update or add tests (`tests/test_compiled_briefings.py` and neighbors).
3. Only then update this skill's files and, if the change is navigational,
   `.claude/docs/prompt-source-map.md`.

## Source Of Truth

### Workflow contracts

- `src/d_brain/control_plane/registry.py` -- `maintenance.compiled-nightly`,
  `maintenance.compiled-fact-check`, and `maintenance.compiled-digest` are
  the three scheduled compile-enrich workflows registered today; several
  question-routing workflows also list `compiled` in
  `required_context`/`allowed_writes`. The on-demand paths below (`/why`,
  "Собрать бриф", the bot's own "Дайджест" and "Сводка недели" buttons)
  still run straight from the CLI/bot layer and do not have their own
  registry entries.

### Core pass

- `src/d_brain/services/compiled_briefings.py` -- `CompiledBriefingService`.
  Owns the refresh queue (`enqueue_refresh`, `drain_queue`,
  `run_queue_worker`), the nightly pass (`run_nightly_maintenance`), page
  classification and routing (`_resolve_targets`, the six
  `COMPILED_BRIEFING_DOMAINS`), the Verify step, trust/conflict rules,
  archival (`_archive_stale_notes`), tier-based compression, and the
  human-owned zone (`HUMAN_ZONE_START` / `HUMAN_ZONE_END`).

### Satellites (pure renderers -- never call a model, and never write except where noted)

- `src/d_brain/services/compiled_enrich_report.py` -- `build_daily_digest`,
  the owner's daily digest.
- `src/d_brain/services/compiled_briefs.py` -- `build_brief`, on-demand
  briefs (decision/topic/project).
- `src/d_brain/services/compiled_why.py` -- `build_why` /
  `build_why_for_path`, provenance for one page.
- `src/d_brain/services/compiled_question_provenance.py` --
  `build_question_provenance`, the "Источники ответа" block rendered under a
  direct-question answer.
- `src/d_brain/services/decisions_queue.py` -- `list_queue_items` /
  `apply_response`, the owner decisions queue (reads/writes
  `.session/decisions-queue.json`, page frontmatter, and archives rejected
  pages -- still fully deterministic, no model calls).
- `src/d_brain/services/compiled_fact_check.py` -- `run_monthly_fact_check`,
  the only satellite in this list that writes: a bounded monthly re-check
  that patches just `last_verified`/`confidence` via frontmatter, or queues
  a `fact-check-rejected` decision instead of silently patching a page that
  fails.

### CLIs

As seen on disk today (`ls src/d_brain/run_compiled_*.py`) -- check disk
before adding new flags here; do not invent a signature:

- `src/d_brain/run_compiled_maintenance.py` -- `--queue-only` (default) /
  `--nightly` / `--initialize-source-state`
- `src/d_brain/run_compiled_pass.py` -- one manual pass over `--date`/
  `--source` (repeatable, behaves identically to a nightly run) /
  `--rollback PASS_ID` / `--dry-run`. The dry run is deliberately narrow: it
  only previews which sources would be enqueued and what is currently in the
  refresh queue -- it never runs compilation and cannot show which compiled
  pages a pass would touch (that resolution is itself a model call).
- `src/d_brain/run_compiled_daily_full_pass.py` -- historical bootstrap
- `src/d_brain/run_compiled_reprocess_days.py` -- manual reprocessing of
  selected days
- `src/d_brain/run_compiled_digest.py` -- `--date` (optional, defaults to
  today) / `--dry-run`
- `src/d_brain/run_compiled_brief.py` -- `--type` (required) / `--query`
  (required) / `--dry-run`
- `src/d_brain/run_compiled_monthly_verify.py` -- `--page-limit`

### Bot surface (owner layer)

- `src/d_brain/bot/handlers/why.py` -- the `/why` command: text-only, asks
  the owner to pick between the candidates instead of guessing on ambiguity
  (two for a close lexical near-tie, but one per domain -- up to six -- when
  the same slug exists in several of them).
- `src/d_brain/bot/handlers/brief.py` -- the free-text step of the "Собрать
  бриф" dashboard flow (the decision/topic/project type buttons live in
  `bot/handlers/menu.py` alongside the rest of the dashboard callbacks).
- `src/d_brain/bot/dashboard.py`, `src/d_brain/bot/handlers/menu.py` -- the
  decisions queue screen, the weekly-review ("Сводка недели") screen,
  brief-type buttons, and digest button.
- Filed artifacts an owner can browse: `summaries/compile/YYYY-MM-DD.md`
  (digest), `summaries/compile/decisions-queue.md` (human-readable decisions
  queue mirror, regenerated on every queue change),
  `summaries/briefs/YYYY-MM-DD-<type>-<slug>.md` (briefs),
  `summaries/consolidations/YYYY/MM/` (cross-source batch consolidations),
  `.session/compile-enrich.json` (pass journal), `.session/decisions-queue.json`
  (open decisions, backing store for the mirror file above),
  `.session/decisions-queue-responses.jsonl` (append-only audit trail of the
  owner's answers on that queue, including "отложить"),
  `.session/compile-dropped-sources.json` (sources the refresh queue
  permanently gave up on -- surfaced in the digest, and an entry is removed
  only once that source finally compiles),
  `.session/compile-fact-check.json` (monthly verification journal, read back
  into the digest for that pass's queue evictions), and
  `.session/compile-queue-worker.json` (why the background queue worker last
  crashed -- also forces a digest, since until it is restarted no new write
  reaches a compiled page).

## Six Domains

Every compiled page lives at `compiled/<domain>/<slug>.md` (or
`compiled/archive/<domain>/<slug>.md` once archived): `projects`, `people`,
`topics`, `decisions`, `meetings`, `concepts`.

Which domain a candidate lands in is decided by the model in the impact
stage; code only checks that the answer is one of the six. `concepts` is the
easiest one to get wrong -- a concept must stay portable, so a candidate
whose title carries a date, or that names one specific project, client, or
person (a fictional example: "Notes on Aurora Solutions Onboarding" belongs
to the `Aurora Solutions` project), is a topic. That is guidance carried in
the impact prompt, not a rule applied to the model's answer afterwards. See
`references/page-schema.md` for the full domain-hint table.

## Owner Layer

The compiled layer is read by, and only by, these owner-facing surfaces --
`/why` for provenance, the "Собрать бриф" flow for on-demand briefs, the
daily digest for what changed, the decisions queue for anything that needs a
human call, and the "Сводка недели" screen that bundles a preview of all
three (queue, changes, a forgotten page) plus one `human_reviewed`
confirmation into a single half-hour weekly pass.

Two queue kinds are not really a human call any more: a `conflict` is
settled by a model when it is created and retried by the nightly pass if
that first attempt came back undecided, and a `drift` suspicion is judged
the same way. Both still appear on the queue screen, and answering one by
hand does exactly what the automated path does -- but the queue is expected
to drain itself. See
`references/links-policy.md` for what each of these does and does not
surface, and `references/conflict-policy.md` for the trust/conflict rules
behind the decisions queue.

## Reference Files

- `references/page-schema.md` -- frontmatter fields (and which ones are
  code-only, never model-written), section order, the six domains,
  concepts-vs-topics routing in full.
- `references/conflict-policy.md` -- source trust levels, claim kinds,
  conflict adjudication and its nightly retry, drift judgement, Verify
  sampling, pass budgets, the decisions queue's nine item kinds.
- `references/links-policy.md` -- the human zone and why link/graph tooling
  must never touch it, the three provenance tables, what `/why` does and
  does not surface.

## Relevant Skills

- [[skills/graph-builder/SKILL|graph-builder]] -- graph analysis; now also
  tracks the compiled domains and the archive folder as their own buckets
- [[skills/vault-health/SKILL|vault-health]] -- deterministic vault
  maintenance; `fix_links.py` now protects the human zone described above
