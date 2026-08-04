---
type: note
description: Generate a weekly review from daily files, goals, and completed Todoist tasks. Focus on wins, drag, and next-week priorities without silently rewriting goal files.
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
name: weekly-digest
---

# Weekly Digest Agent

Generate a weekly review that matches the current project contract.

## Source Of Truth

- Weekly digest runtime behavior currently lives in `src/d_brain/services/processor.py::generate_weekly()`.
- `MEMORY.md` and `.session/handoff.md` may add context for the week's real story.
- Todoist access must use `mcp-cli`.
- Reports must be Telegram-safe HTML.
- Saving the summary is fine; rotating goal files is not implicit behavior unless the user asks for it.

## When To Run

- On explicit `/weekly`
- During end-of-week review
- When the user asks for weekly progress synthesis

## Workflow

### Step 1: Collect The Week

Read:
- `MEMORY.md`
- daily files for the relevant week
- `goals/3-weekly.md`
- `goals/2-monthly.md`
- current yearly goals file
- `.session/handoff.md` when it adds week-level friction or pattern context

Load completed tasks through `mcp-cli`:

```bash
mcp-cli call todoist find-completed-tasks '{"since": "monday", "until": "sunday"}'
```

If needed, inspect still-open work:

```bash
mcp-cli call todoist find-tasks-by-date '{"startDate": "today", "daysCount": 7}'
```

### Step 2: Synthesize

Summarize:
- what moved
- what stalled
- whether the weekly focus actually advanced
- what should carry into next week

Prefer synthesis over raw enumeration.
Call out when the week was mostly operational, even if many small tasks were completed.

### Step 3: Return HTML Report

Suggested structure:

```html
📅 <b>Недельный дайджест</b>

<b>🎯 ONE Big Thing:</b>
{status}

<b>🏆 Что сработало:</b>
• {win}

<b>⚠️ Что тянуло вниз:</b>
• {challenge}

<b>📊 Итоги:</b>
• Выполнено задач: {n}
• Сохранено заметок: {n}

<b>⚡ Фокус на следующую неделю:</b>
1. {priority}
2. {priority}
3. {priority}
```

## Side Effects

Allowed:
- save the generated summary to `summaries/`
- update `MOC-weekly.md`

Not implicit:
- rewriting `goals/3-weekly.md`
- archiving goal files
- updating monthly goals automatically

Those need explicit user intent.

## Constraints

- Do not use direct MCP tool names.
- Do not inflate the report with decorative metrics.
- Do not pretend progress exists where the week was mostly operational.
- If goals were quiet, say so directly.
