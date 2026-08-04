---
type: note
last_accessed: 2026-05-05
relevance: 0.36
tier: archive
paths: "goals/**/*.md"
---

# Goals Format

Navigation rule for goal files in `goals/`.

This file should stay descriptive, not prescriptive.
Runtime goal usage lives in:
- `skills/dbrain-processor/references/goals.md`
- `src/d_brain/services/processor.py`

## Current Goal Files

```
goals/
├── 0-vision-3y.md
├── 1-yearly-YYYY.md
├── 2-monthly.md
└── 3-weekly.md
```

## Runtime Read Order

1. `goals/3-weekly.md`
2. `goals/2-monthly.md`
3. current yearly goals file
4. `goals/0-vision-3y.md` when broader direction matters

Weekly and monthly files carry the highest operational weight during capture and execution.

## Common Fields

The live goal files currently use lightweight frontmatter such as:
- `type`
- `updated`
- `period` or `week`
- memory fields managed by the decay engine

Do not introduce a second mandatory schema here unless the live goal files actually change.

## ONE Big Thing

`goals/3-weekly.md` must expose one clearly readable `## ONE Big Thing` section.
The processor reads it as the top weekly anchor for capture, preview, and reflect.
Keep it visible near the top and phrased as one operational sentence, not a vague slogan.

## Linking Guidance

- Link saved thoughts to goals only when the relationship is real and specific.
- Prefer the closest active goal note or section.
- Avoid decorative goal links that do not change retrieval or decision quality.

## When Behavior Changes

If runtime goal logic changes, update the canonical runtime files first instead of expanding this rule into a second workflow spec.
