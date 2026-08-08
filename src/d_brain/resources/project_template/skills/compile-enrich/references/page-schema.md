---
type: note
description: Field-level schema for one compiled/ page -- the six domains, the concepts-vs-topics routing override, frontmatter fields, and section order. Mirrors src/d_brain/services/compiled_briefings.py; do not restate it as an independent spec.
last_accessed: 2026-08-05
relevance: 0.6
tier: warm
---

# Page Schema

This describes the shape `CompiledBriefingService` renders. It is documentation of
the code, not a second definition of it -- when in doubt, read
`src/d_brain/services/compiled_briefings.py` directly.

## Six Domains

Every compiled page lives under `compiled/<domain>/<slug>.md` (or
`compiled/archive/<domain>/<slug>.md` once archived). `COMPILED_BRIEFING_DOMAINS`:

| Domain | What belongs here |
|---|---|
| `projects` | long-running projects, initiatives, clients, pipeline, product threads |
| `people` | people, contacts, partners, clients, teams, important relationships |
| `topics` | durable research themes, directions, recurring topics |
| `decisions` | decisions with consequences, agreements, commitments, constraints |
| `meetings` | recurring meeting series, important negotiation tracks, recurring calls |
| `concepts` | durable concepts, methods, models, approaches -- portable across projects and clients, not tied to one specific project, client, or date |

## Concepts vs Topics

A candidate the model proposes for `concepts` is rerouted to `topics` (never
the reverse) whenever `_concepts_to_topics_reason` finds either signal:

1. **The title contains a date** -- an ISO date (`2026-08-05`) or a Russian
   long-form date (`5 августа 2026`). A dated concept discussion is really a
   topic snapshot, not a portable, date-free concept.
2. **The title fully contains an existing project/people page's name** (by
   that page's own title tokens or slug tokens). Example: a project page
   named "Aurora Solutions" exists, and a new concepts candidate is titled
   "Notes on Aurora Solutions Onboarding" -- that candidate is rerouted to
   `topics` because it is really about one specific project, not a portable
   concept.

Exception to signal 2: a page name that is exactly **one generic common
noun** (`GENERIC_SINGLE_TOKEN_PAGE_NAMES` -- words like "платформа",
"команда", "migration", "system", "process") does not trigger the reroute on
its own, because matching a common noun is coincidence, not a real binding
to that project/person. A one-word **proper** noun (a fictional project like
"Aurora", a fictional client like "Northbridge") still triggers it. A
multi-word name is unaffected either way -- a full multi-word match is
already a strong signal.

## Frontmatter Fields

Rendered fresh on every `_upsert_briefing` call. Split by who decides the value:

**From the model's compile output** (still validated/clamped by code):
`description`, `status`, `freshness_state`, `confidence`, and, when
`record_kind` is `decision` or `incident`, the matching decision/incident
sub-fields (`decision_status`, `decision_owner`, `decision_date`, `severity`, ...).

**Set only by code, never by the model** (see the field comment above
`sources_trust` in `compiled_briefings.py`'s `_render_briefing`):
- `sources_trust` -- `own | forwarded | integration | inferred`; the minimum
  trust level among the page's sources so far.
- `last_verified` -- `YYYY-MM-DD` or empty; when the page's claims were last
  confirmed (by a fresh claim landing, or by the monthly fact-check).
- `enrichment_count` -- how many times this page has been enriched.
- `conflicts_open` -- number of unresolved factual conflicts.
- `human_reviewed` -- `YYYY-MM-DD` or empty; when the owner last confirmed
  this page (a date, not a boolean).
- `quality_status` -- `needs_review`; present only when Verify found a
  content-quality problem in the version that was still saved.
- `quality_reason` -- the failed checks or other content-quality reasons.
  Both quality fields disappear after the next successful Verify pass.

Generation logic (`add_descriptions.py` and friends in `vault-health`) must
never guess at these five fields for a compiled page -- see
`vault-health/SKILL.md`'s anti-patterns.

## Section Order

For `record_kind: decision`, before everything below: `## Decision Record`,
`## Rationale`, `## Alternatives Considered`, `## Decision Evidence`.

For `record_kind: incident`, before everything below: `## Incident Debrief`,
`## Timeline`, `## Root Cause`, `## What Worked`, `## What Did Not Work`,
`## Corrective Actions`, `## Generalizable Learning`.

Common tail, every page:

1. `## Current State`
2. `## Recent Changes`
3. `## Open Loops`
4. `## Key Decisions`
5. `## Next Check`
6. `## Sources`
7. `## Sources That Shaped This Page` (table)
8. `## Open Conflicts` (table)
9. `## Claim History` (table)
10. `## History` (only once cooled entries have been compressed off Recent
    Changes / Open Loops)
11. `## Owner Notes` (wraps the human zone -- see `references/links-policy.md`)

`lint_notes()` only requires the minimum set `Current State`, `Recent
Changes`, `Open Loops`, `Key Decisions`, `Next Check`, `Sources` -- the rest
are populated as the page accumulates provenance and owner activity.
