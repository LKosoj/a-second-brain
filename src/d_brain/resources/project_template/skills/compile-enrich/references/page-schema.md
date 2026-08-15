---
type: note
description: Field-level schema for one compiled/ page -- the six domains, concepts-vs-topics routing, frontmatter fields, and section order. Mirrors src/d_brain/services/compiled_briefings.py; do not restate it as an independent spec.
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

The domain a candidate lands in is the model's call, made once in the impact
stage. Code validates that the answer is one of the six domains and nothing
more -- it does not second-guess which one.

Earlier versions rerouted `concepts` to `topics` deterministically (a date
regex on the title, plus a token-subset match against existing
project/people page names, minus a common-noun stoplist). That override is
gone: it outranked the model's answer using far less context than the model
had, and no amount of stoplist tuning fixed the cases where a genuinely
portable concept happened to mention a project by name.

The reasoning behind it now reaches the model as guidance in the impact
prompt instead (`_build_impact_prompt`): concepts must stay portable, so a
title carrying a date, or naming one specific project, client, or person
from the catalog, is a topic rather than a concept. The model weighs that
against the actual source and decides.

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
- `quality_status` -- `needs_review`; present when Verify found a
  content-quality problem in the version that was still saved, or when Verify
  produced no usable reply at all (unparseable JSON, missing `page_checks` or
  `page_issues`) so the claims went in unverified.
- `quality_reason` -- the failed checks, other content-quality reasons, or the
  reason Verify could not be used.
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
