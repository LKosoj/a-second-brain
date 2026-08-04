---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Phase 2: EXECUTE

Read capture.json from Phase 1. Create Todoist tasks, save thoughts, update business/project context, and report the exact changes.

## Input
- `.session/capture.json` — output from Phase 1
- `business/crm.md`, `business/network.md`, `business/events.md` — business work areas
- `projects/clients.md`, `projects/leads.md`, `projects/projects.md` — project work areas

The business and projects indexes are already in the injected core context.

## Task

Before writing anything:
- decide the minimal set of writes needed for this run;
- prefer updating an existing durable note over creating a near-duplicate;
- if uncertainty stays high, keep it in `observations` instead of forcing a task/note/CRM write.

### 1. Create Todoist tasks

For each entry with `classification: "task"`:
- Choose `projectId` from the provided live Todoist project catalog when the fit is clear.
- Inbox is fallback only.
- Personal tasks may still belong to named personal projects.
- Create the owner's controllable next step, not a vague outcome statement.
- If the note implies a reusable cadence, prefer a process-goal style recurring task.

```bash
mcp-cli call todoist add-tasks '{"tasks": [{"content": "...", "dueString": "...", "priority": "p2"}]}'
```

Record created task IDs.

### 2. Check process goals

```bash
mcp-cli call todoist find-tasks '{"labels": ["process-goal"]}'
```

If missing or stale, create or refresh them from the current weekly/monthly goals and the supplied process-goals reference.

### 3. Save thoughts

For each entry with classification `idea`, `reflection`, `learning`, or `project`:
- Create a file in `thoughts/{category}/YYYY-MM-DD-slug.md`
- Use the agent-memory card contract:
  - `description` must read like a retrieval snippet
  - keep tags sparse and meaningful
  - add `source: daily/{DATE}.md`
- If the same durable claim already exists in the vault, update or link it instead of creating a duplicate note.
- Add wiki-links only for real contextual relationships
- Add typed relationships in `## Related`:
  ```markdown
  ## Related
  - [[business/_index|Business Index]] — context: client thread mentioned in the note
  ```

### 4. Update CRM

For entries with `classification: "crm_update"`:
- Update the most relevant existing business/project file:
  - `business/crm.md`
  - `business/network.md`
  - `business/events.md`
  - `projects/clients.md`
  - `projects/leads.md`
  - `projects/projects.md`
- Update only when the entry materially changes what is true.
- If the current flat files clearly need a new linked note, create one conservatively and link it from the proper index instead of inventing a deep path.

### 5. Build links

For all created/updated files:
- Check `MOC/index.md` and the nearest domain hub before adding broader links
- Add only justified wiki-links with context phrases
- Keep the link graph sparse and meaningful
- Update frontmatter `related: []` only when it improves retrieval

### 6. Check workload

```bash
mcp-cli call todoist find-tasks-by-date '{"startDate": "today", "daysCount": 7}'
```

## mcp-cli retry algorithm

```
1. Call mcp-cli
2. If it fails, wait briefly and re-read local context
3. Retry a second time
4. If it fails again, wait a bit longer
5. Retry a third and final time
```

Never claim Todoist is unavailable before the retries are exhausted.
If it still fails after the third attempt, keep the exact error text in `observations`.

## Output Format

Print ONLY valid JSON:

```json
{
  "tasks_created": [
    {"id": "8501234567", "content": "Follow-up Acme Corp", "priority": 2, "due": "tomorrow"}
  ],
  "thoughts_saved": [
    {"path": "thoughts/ideas/2026-02-19-layered-memory.md", "title": "AI agents need layered memory", "category": "ideas"}
  ],
  "crm_updated": [
    {"path": "business/crm.md", "change": "Added note about Animaccord follow-up"}
  ],
  "links_created": [
    {"from": "thoughts/ideas/2026-02-19-layered-memory.md", "to": "business/_index.md", "context": "context: linked to current client thread"}
  ],
  "process_goals": {
    "active": 5,
    "overdue": 1,
    "created": 0
  },
  "workload": {
    "mon": 3, "tue": 2, "wed": 4, "thu": 1, "fri": 2, "sat": 0, "sun": 0
  },
  "observations": []
}
```
