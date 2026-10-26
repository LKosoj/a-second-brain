---
type: note
description: Generate a weekly review from daily files, goals, and completed Todoist tasks. Focus on wins, drag, and next-week priorities without silently rewriting goal files.
last_accessed: 2026-08-30
relevance: 1.0
tier: active
name: weekly-digest
---

# Weekly Digest Agent

Generate a weekly review that matches the current project contract.

## Source Of Truth

- Weekly digest runtime behavior currently lives in `src/d_brain/services/processor.py::generate_weekly_digest()`.
- `MEMORY.md` and `.session/handoff.md` may add context for the week's real story.
- Todoist access must use `mcp-cli`.
- Reports must be markdown (the runtime converts it for Telegram).
- Saving the summary is fine; rotating goal files is not implicit behavior unless the user asks for it.

## When To Run

- When the weekly digest workflow runs (scheduled cycle)
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

### Step 3: Return Markdown Report

Suggested structure:

```markdown
📅 **Недельный дайджест**

**🎯 ONE Big Thing:**
{status}

**🏆 Что сработало:**
• {win}

**⚠️ Что тянуло вниз:**
• {challenge}

**📊 Итоги:**
• Выполнено задач: {n}
• Сохранено заметок: {n}

**⚡ Фокус на следующую неделю:**
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
