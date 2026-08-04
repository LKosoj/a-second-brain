---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Direct Question Answering

Answer the user's question now instead of capturing it as a note.

## Read Before Answering

- `MEMORY.md`
- active goal files
- `business/_index.md` and `projects/_index.md` when work context matters
- Todoist state when priorities, deadlines, workload, or completion status matter

## Answer Rules

- synthesize, do not dump raw notes
- answer the actual question in the first sentence before giving surrounding context
- keep the answer concise and actionable by default
- when the answer depends on live state, say briefly what you checked
- if uncertainty remains, state it briefly
- if a needed source is missing, say so instead of guessing
- do not narrate the retrieval process unless it changes the answer
- for fact, status, and history answers, finish with `Источники:` and 2-5
  vault-relative `[[wikilinks]]` to files actually read
- cite source notes, not search-result snippets; never invent a citation
- when fewer than two confirming sources exist, list only the real source(s),
  state the evidence gap briefly, and mark unsupported conclusions as inference

## Status And History Questions

When the user asks for the status or history of a project or other long-running topic:
- retrieve and synthesize the history of the topic, not only the latest snapshot
- if the topic spans weeks or months, include enough depth to explain why the current status is what it is
- cover: current status, what changed recently, key milestones over time, blockers/risks, and next steps
- separate current facts from older milestones when they differ
- do not collapse a long-running project into a very short answer if important history exists

## Priority Questions

When asked about priorities:
- combine goals, active tasks, and recent context
- prefer current commitments over abstract possibilities
- call out overload or deadline risk when it is real
