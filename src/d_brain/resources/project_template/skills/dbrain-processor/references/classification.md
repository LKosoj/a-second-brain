---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Entry Classification

Classify by meaning and commitment, not by word lists.

## Source of Truth

Before classifying, rely on:
- `about.md` — who the user is and what kinds of work matter
- current goal files
- `business/_index.md` and linked cards for live business context
- `projects/_index.md` and linked notes for live project context

## Core Classes

- `task` — there is a concrete obligation, commitment, follow-up, deadline, or next action
- `idea` — there is a promising concept worth preserving, but not yet a concrete obligation
- `reflection` — there is a personal conclusion, judgement, or self-observation
- `learning` — there is reusable knowledge, a pattern, or an explanation worth revisiting
- `project` — there is a durable initiative or workstream larger than one action
- `crm_update` — the note changes what is true about a client, relationship, deal, or business thread
- `skip` — already processed, duplicate, empty, or not worth downstream work

## Classification Heuristics

### Choose `task` when
- the user or another clearly identified owner has to do something,
- there is an explicit or strongly implied next action,
- or delaying the note creates operational risk.

### Choose `idea` when
- the value is in preserving a concept or possibility,
- and immediate execution is optional.

### Choose `reflection` when
- the note is mainly about self-observation, judgement, energy, values, or relationships.

### Choose `learning` when
- the note captures a reusable lesson, comparison, pattern, or explanation.

### Choose `project` when
- the note defines or reshapes an ongoing initiative,
- groups multiple tasks under one direction,
- or represents a long-running track that deserves its own note context.

### Choose `crm_update` when
- the note materially updates a company, partner, deal, contact, or active relationship,
- even if it does not create a new task.

## Business and Project Detection

Do not rely on static keyword lists.

Treat an entry as business/project-related when:
- it clearly refers to an entity already present in `business/` or `projects/`,
- or it describes a live workstream, client thread, relationship, or delivery context that belongs there.

If the entity is ambiguous:
- prefer an existing vault entity only when the match is strong,
- otherwise keep the note generic and avoid inventing links.

## Urgency and Priority Signals

Do not assign urgency from isolated words alone.

Infer urgency from:
- explicit time constraints,
- external commitments,
- dependency chains,
- delivery risk,
- and impact on current goals or active work.

## Output Locations

| Category | Destination |
|----------|-------------|
| `task` | Todoist |
| `idea` | `thoughts/ideas/` |
| `reflection` | `thoughts/reflections/` |
| `learning` | `thoughts/learnings/` |
| `project` | `thoughts/projects/` |

## Thought Quality Rules

When saving non-task thoughts:
- write a claim, not a topic label;
- keep `description` retrieval-friendly;
- add links because of real contextual relationships, not lexical overlap;
- prefer one strong note over several weak duplicates.

## Anti-Patterns

Avoid:
- classifying by surface keywords instead of meaning,
- turning vague worry into a task without a concrete next step,
- creating a project note for a single atomic action,
- creating CRM updates when the note only mentions a company in passing,
- and saving academic theory without application to real work or life context.
