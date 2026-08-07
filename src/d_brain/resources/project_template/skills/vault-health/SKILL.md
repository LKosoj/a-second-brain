---
type: note
description: Vault health routing doc for graph score checks, MOC regeneration, backlinks, broken-link repair, and weekly system reflection around the current flat vault layout.
last_accessed: 2026-07-09
relevance: 0.78
tier: warm
name: vault-health
depends_on: [graph-builder]
---

# Vault Health

Routing doc for vault-health maintenance around the live repo layout.

## What This Skill Owns

- health-score checks and interpretation
- MOC regeneration
- backlinks and broken-link repair
- weekly reflection over observations and graph history

## Quick Commands

```bash
uv run skills/graph-builder/scripts/analyze.py vault
uv run skills/vault-health/scripts/generate_moc.py
uv run skills/vault-health/scripts/add_descriptions.py
uv run skills/vault-health/scripts/add_descriptions.py --apply
uv run skills/vault-health/scripts/connect_orphans.py
uv run skills/vault-health/scripts/connect_orphans.py --apply
bash skills/vault-health/scripts/backlinks.sh "business/crm"
uv run skills/vault-health/scripts/fix_links.py
uv run skills/vault-health/scripts/fix_links.py --apply
```

## Current Vault Topology

Supported top-level work areas today:
- `business/_index.md`, `business/crm.md`, `business/network.md`, `business/events.md`
- `projects/_index.md`, `projects/clients.md`, `projects/leads.md`, `projects/projects.md`
- `MOC/MOC-ideas.md`, `MOC/MOC-learnings.md`, `MOC/MOC-plaud.md`, `MOC/MOC-projects.md`, `MOC/MOC-reflections.md`, `MOC/MOC-weekly.md`, `MOC/index.md`
- `compiled/projects/`, `compiled/people/`, `compiled/topics/`, `compiled/decisions/`, `compiled/meetings/`, `compiled/concepts/` -- the six compile-enrich domains, plus `compiled/archive/<domain>/` for archived pages. See [[skills/compile-enrich/SKILL|compile-enrich]] for what maintains this layer.

Do not describe old nested business/project trees as if they were current truth.

## Health Targets

- higher score is better, but trend matters more than any single run
- broken links should trend down
- orphan count should trend down outside expected areas like `daily/`
- description coverage should trend up on durable cards
- a `compiled/archive/<domain>/` page with zero incoming links is
  **expected**, not a defect: no incoming links is the archival trigger
  itself (see `compile-enrich/SKILL.md`), so `analyze.py` reports it
  separately from real orphans instead of counting it as one

## Compiled Layer Frontmatter

Five `compiled/` frontmatter fields -- `sources_trust`, `last_verified`,
`enrichment_count`, `conflicts_open`, `human_reviewed` -- are set only by
code inside `CompiledBriefingService`, never by heuristics. `add_descriptions.py`
and any similar description-generation tooling must never guess at or
overwrite these five fields on a compiled page; see
[[skills/compile-enrich/SKILL|compile-enrich]] for what each one means.

## Usage Notes

- Run `analyze.py` before judging health or changing links.
- Use `generate_moc.py` only when the resulting MOCs still match the live vault structure.
- Use `add_descriptions.py` in dry-run first, then `--apply` only after checking projected coverage.
- Use `connect_orphans.py` only after `analyze.py`, and apply it only when orphan or weak-connection output still makes sense.
- Use `fix_links.py` in dry-run first.
- Use `backlinks.sh` with real current note paths or note stems.
- Weekly reflection should turn repeated observations into concrete file-level proposals with the smallest useful change.

## Anti-Patterns

- treating stale script assumptions as canonical truth
- running repair scripts blindly without reading their output
- inventing nested CRM/lead paths that do not exist in the current vault
- using health score as vanity metric instead of a maintenance signal
- collapsing the six nested `compiled/<domain>/` paths (or
  `compiled/archive/<domain>/`) the way legacy nested business/project paths
  get collapsed -- these subdirectories are current truth, not legacy debt
- editing the content of a compiled page's human zone (between
  `<!-- human:start -->` and `<!-- human:end -->`, inside `## Owner Notes`)
  -- that zone is owner-written and every maintenance script must leave it
  byte-for-byte untouched

## Relevant Skills

- [[skills/graph-builder/SKILL|graph-builder]] — core graph analysis
- [[skills/dbrain-processor/SKILL|dbrain-processor]] — daily processing pipeline
- [[skills/compile-enrich/SKILL|compile-enrich]] — compiled/ layer this skill's scripts now recognize
