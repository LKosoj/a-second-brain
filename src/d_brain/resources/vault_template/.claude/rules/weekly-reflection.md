---
type: note
last_accessed: 2026-04-03
relevance: 0.26
tier: archive
paths: "thoughts/reflections/YYYY-W*"
---

# Weekly Reflection Format

Navigation rule for weekly system reflections.

This file should stay lightweight.
Runtime-adjacent logic lives in:
- `skills/vault-health/SKILL.md`
- `vault/.claude/rules/telegram-report.md`
- `vault/.session/handoff.md`
- `vault/.graph/health-history.json`

## Purpose

Analyze accumulated observations, find recurring friction, and propose concrete maintenance changes.

## When to Generate

- Every Sunday from the scheduled daily orchestrator
- When observations in `.session/handoff.md` reach ≥10

## File Location

`thoughts/reflections/YYYY-WNN-system-reflection.md`

## Required Sections

A useful reflection usually contains:
- friction patterns
- improvements already applied
- next proposals with target files
- graph-health deltas
- note that processed observations were cleared or retained

## Process

1. Read `.session/handoff.md` → collect `## Observations`
2. Read `.graph/health-history.json` → get this week's trend
3. Group observations by type (friction/pattern/idea)
4. Find recurring patterns (same issue >2 times)
5. Propose concrete improvements (file + change)
6. Write reflection file
7. Clear processed observations from handoff.md (keep unprocessed)
8. Add reflection to daily log
9. In the scheduled Sunday cycle, switch `goals/3-weekly.md` to the next ISO week after reflection completes without error

Prefer recurring operational patterns over one-off incidents.
Every proposal should name the exact file and the smallest useful change.
After cleanup, `handoff.md` should still have one rolling status block and no more than 10 unresolved observations.

## Tags

Use `#system` tag for all system reflections.
Cross-reference with:
- `[[MEMORY.md]]` — if proposals become decisions
- `[[goals/3-weekly]]` — if proposals affect weekly focus

## When Behavior Changes

If the weekly-reflection workflow changes, update the active skill/runtime docs first instead of adding another full template here.
