---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Interactive Preview Phase

Read capture results and generate a quick markdown preview for Telegram.

## Input
- Inline `capture.json` payload provided in the prompt

## Task

1. Summarize what was captured from `daily/{DATE}.md`
2. Highlight:
   - ONE Big Thing alignment
   - candidate tasks that would be created
   - candidate thoughts/CRM updates that would be saved
   - skipped entries, if any
3. End with the next recommended action for the user

## Hard Rules

- This is PREVIEW ONLY
- Do NOT create Todoist tasks
- Do NOT write or edit vault files
- Do NOT update MEMORY.md
- Do NOT append to daily or handoff
- Do NOT claim actions were completed; describe what would happen
- If capture produced little or nothing actionable, say that directly instead of padding the preview

## Output Format

Return markdown only:
- No HTML tags
- No tables
- Start with `# ⚡ Быстрый разбор за {DATE}` or an equivalent short heading
- Keep it concise for Telegram
