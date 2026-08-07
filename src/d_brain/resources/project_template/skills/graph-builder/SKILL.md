---
type: note
description: Analyze the vault graph, inspect orphan and broken-link reports, and add only justified links or frontmatter relations aligned with the current flat business/projects layout.
last_accessed: 2026-06-13
relevance: 0.56
tier: cold
name: graph-builder
allowed-tools: Bash(uv run:*), Bash(rg:*), Read, Edit
depends_on: []
---

# Graph Builder

Routing doc for vault graph analysis and targeted link-building.

## What This Skill Owns

- deterministic graph analysis via `analyze.py`
- targeted semantic links after reading the report
- conservative frontmatter `related` updates

## Current Workflow

1. Run:
   ```bash
   uv run skills/graph-builder/scripts/analyze.py vault
   ```
2. Read:
   - `vault/.graph/vault-graph.json`
   - `vault/.graph/report.md`
3. Prioritize non-daily orphan or weakly connected files.
4. Read `MOC/index.md` and the nearest domain hub before proposing links.
5. Add only links you can justify from actual content.

## Current Domains

### Personal
- `thoughts/`
- `goals/`
- `daily/`
- Hub: `MEMORY.md`

### Business
- `business/_index.md`
- `business/crm.md`
- `business/network.md`
- `business/events.md`
- Hub: `business/_index.md`

### Projects
- `projects/_index.md`
- `projects/clients.md`
- `projects/leads.md`
- `projects/projects.md`
- Hub: `projects/_index.md`

### Compiled

- `compiled/projects/`, `compiled/people/`, `compiled/topics/`,
  `compiled/decisions/`, `compiled/meetings/`, `compiled/concepts/`, and
  `compiled/archive/<domain>/` for archived pages -- `analyze.py` buckets
  each of these as its own domain (`compiled/<domain>`,
  `compiled/archive/<domain>`) instead of collapsing them under one
  `compiled` row.
- Hub: none. Unlike Business/Projects, the compiled domains have no single
  index note to link back to -- see
  [[skills/compile-enrich/SKILL|compile-enrich]] for how these pages are
  actually maintained.

## Link Quality Rules

- link from meaning, not from name similarity alone
- prefer existing hub/index notes when entity structure is still shallow
- use frontmatter `related` only when it improves retrieval
- keep 2-5 strong links instead of 10 weak ones
- daily files are low-priority for manual graph cleanup
- prefer one targeted cleanup pass over bulk relinking
- the "Sources That Shaped This Page", "Claim History", and "Open
  Conflicts" tables on a compiled page already carry their reason in an
  adjacent cell -- do not run mention-based "link by meaning" logic over
  those rows; they are a generated ledger, not links to curate (see
  [[skills/compile-enrich/SKILL|compile-enrich]])

## Do Not

- do not change file bodies unless the task explicitly requires it
- do not add links to non-existent files
- do not invent old nested paths such as `business/crm/*` or `projects/leads/*`
- do not create new files without a strong retrieval reason
- do not treat a `compiled/archive/<domain>/` page with no incoming links as
  a graph defect -- no incoming links is the archival trigger itself, not a
  broken-graph symptom (`analyze.py` already reports it separately)
- do not edit the content of a compiled page's human zone (between
  `<!-- human:start -->` and `<!-- human:end -->`) -- that text is
  owner-written and out of scope for graph maintenance. `add_links.py`
  enforces this in code (`human_zone_span`): it skips a `## Related`
  heading inside the zone and leaves a file with ambiguous markers
  untouched, the same way `vault-health/scripts/fix_links.py` does
- do not carry the "collapse nested legacy paths" habit from
  `business/*`/`projects/*` over to `compiled/*` -- the nested
  `compiled/<domain>/` and `compiled/archive/<domain>/` paths are current
  truth, not legacy debt to flatten

## Relevant Skills

- [[skills/vault-health/SKILL|vault-health]] — health scoring, MOC generation, link repair
- [[skills/dbrain-processor/SKILL|dbrain-processor]] — daily processing pipeline
- [[skills/compile-enrich/SKILL|compile-enrich]] — the compiled/ layer this skill's `analyze.py` now recognizes
