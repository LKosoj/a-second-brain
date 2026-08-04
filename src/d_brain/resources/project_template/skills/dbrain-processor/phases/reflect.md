---
type: note
last_accessed: 2026-04-03
relevance: 0.26
tier: archive
---
# Phase 3: REFLECT

Read execute results. Generate a markdown report. Update MEMORY. Write observations.

## Input
- `.session/capture.json` — from Phase 1
- `.session/execute.json` — from Phase 2
- `.session/memory-audit.md` — duplicate/overlap audit for MEMORY.md (when present)
- `.graph/health-history.json` — vault health trend

`MEMORY.md` and the rolling handoff are already in the injected core context.

## Task

Execution order:
1. build the Telegram report;
2. make only surgical edits to `MEMORY.md`;
3. append new observations to handoff without duplicating old ones;
4. rewrite the rolling handoff status fields in place.

Do not rewrite whole files when a small append or bullet replacement is enough.
Preserve older handoff observations unless they are explicitly rolled into a weekly reflection.
`handoff.md` is a rolling short-term context file, not an append-only session log.
The runtime appends the bounded daily summary block after this phase.
Do not edit `daily/{DATE}.md` directly from this prompt.

### 1. Generate markdown report

Build one concise Telegram markdown report. Include:

- ONE Big Thing (from capture.json)
- Thoughts saved (from execute.json)
- Tasks created (with IDs)
- Process goals status
- Workload by day
- Top 3 priorities
- Observations (if any)

Recommended section order:
1. headline for the day
2. top priorities
3. tasks created
4. thoughts saved
5. business/project updates
6. process goals and workload
7. observations or next-step warnings

### 2. Evolve MEMORY.md

Check if any information from today deserves long-term memory:
- New key decisions
- Pipeline changes (new lead, closed deal, status change)
- Financial changes
- Active Context updates

Rules:
- New info REPLACES outdated (don't append duplicates)
- Only write significant changes
- Default to NO edit when unsure
- Only keep facts that should still matter weeks or months later
- Before editing, read `.session/memory-audit.md` if it exists and collapse flagged overlaps
- `Ключевые решения` = durable policy / architecture / source-of-truth decisions
- `Правила для запоминания` = short heuristics and invariants only
- Do NOT keep the same knowledge in both sections
- If several bullets describe the same policy through different entrypoints/examples, merge them into one canonical statement
- Move transient debugging forensics, one-off metrics, and workaround history out of `MEMORY.md`
- Prefer the smallest edit that restores one clean canonical statement
- If an item is mainly useful for the current session, today only, or the next handoff, keep it in `daily` or `.session/handoff.md` instead
- Do NOT write step-by-step implementation progress, one-off smoke tests, file inventories, temporary migration status, or short-lived next actions into `MEMORY.md`
- When a durable fact is corrected, create a successor note with valid namespaced
  epistemic metadata and run
  `uv run skills/agent-memory/scripts/memory-engine.py supersede OLD NEW --vault vault`.
  Do not silently overwrite or delete the older fact; keep its body unchanged.
  Run it only while non-cooperative vault editors are quiesced: the shared
  vault-write lock coordinates services, but cannot provide absolute CAS
  protection against an external editor that ignores it.

### 3. Capture observations

If problems occurred during processing, append to `.session/handoff.md` under `## Observations`:

```markdown
- [friction] 2026-02-19: mcp-cli timeout on todoist — retried 3x
- [pattern] 2026-02-19: daily had only 2 entries — low activity day
```

### 4. Update handoff.md

Update session context:
- Last Session: what was processed
- Key Decisions: if any
- In Progress: incomplete items
- Next Steps: what to do next

Hard rules:
- Keep exactly one instance of each heading:
  `## Last Session`, `## Key Decisions`, `## In Progress`, `## Next Steps`, `## Observations`
- Overwrite `Last Session`, `Key Decisions`, `In Progress`, and `Next Steps` instead of appending another session block
- `Observations` may accumulate unresolved carry-over items, but deduplicate them
- Keep at most 10 observation bullets in handoff; drop older processed items first
- If an observation was already rolled into a weekly reflection, remove it from handoff
- Keep the file compact enough to scan in under a minute

## Output Format

Return markdown only. The runtime converts it to Telegram formatting.

Markdown constraints:
- No HTML tags
- No tables
- Keep the report compact enough for Telegram

## CRITICAL

- Output is markdown only
- No HTML tags anywhere
- Keep the report readable in Telegram without relying on a hidden template elsewhere
