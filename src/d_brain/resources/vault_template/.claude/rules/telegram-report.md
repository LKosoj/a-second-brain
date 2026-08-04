---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
paths: "**/REPORT*.md"
---

# Telegram Report Formatting

Transport-level formatting rules for Telegram markdown output.

This file is not the canonical report-content template.
Content decisions live in:
- `skills/dbrain-processor/phases/preview.md`
- `skills/dbrain-processor/phases/reflect.md`
- `src/d_brain/services/processor.py`

Safety constraints are enforced by:
- `src/d_brain/bot/formatters.py`

## CRITICAL: Output Format

**Return markdown only. No HTML tags.**

WRONG:
```html
<b>Title</b>
```

CORRECT:
```md
**Title**
```

Short outputs go to Telegram as `parse_mode=MarkdownV2`. Long outputs may be
converted to HTML only at the final transport layer.

## Allowed Markdown

- `**bold**`
- `*italic*`
- `` `code` ``
- `~~strikethrough~~`
- `[text](url)` links
- headings, bullets, numbered lists, quotes

## FORBIDDEN

- HTML tags
- Tables (not supported by Telegram)
- Unsupported markdown extensions
- Unsupported tags: `<strong>`, `<em>`

## Validation Rules

- Keep markdown simple enough for deterministic runtime conversion.
- Keep output concise: Telegram hard limit is 4096 characters.
- Prefer short sections over dense walls of text.
- The formatter may convert legacy HTML, but prompts should not rely on that fallback.

## Scope

Apply these rules to:
- daily preview output
- reflect-phase reports
- `/do` status replies
- weekly digest output
- direct question answers returned as Telegram markdown

## When Behavior Changes

If report content or section logic changes, edit the canonical runtime files first instead of adding another full template here.
