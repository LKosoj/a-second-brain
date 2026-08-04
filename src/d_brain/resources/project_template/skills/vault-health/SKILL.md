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

Do not describe old nested business/project trees as if they were current truth.

## Health Targets

- higher score is better, but trend matters more than any single run
- broken links should trend down
- orphan count should trend down outside expected areas like `daily/`
- description coverage should trend up on durable cards

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

## Relevant Skills

- [[skills/graph-builder/SKILL|graph-builder]] — core graph analysis
- [[skills/dbrain-processor/SKILL|dbrain-processor]] — daily processing pipeline
