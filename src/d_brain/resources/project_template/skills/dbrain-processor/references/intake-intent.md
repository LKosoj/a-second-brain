---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
Route plain Telegram text into one of two intents:

- `capture` — user is sending information to save into the second brain
- `question` — user expects a direct answer now

Use semantics, not keyword lists.

Choose `question` only on high confidence. When ambiguous, choose `capture`.

Typical `capture` signals:
- personal note, idea, observation, reminder, log
- material to remember later
- content that should become part of the archive

Typical `question` signals:
- user is asking for current status, priorities, summary, lookup, explanation, recommendation, comparison, or decision support
- user expects an answer now rather than archival storage

Important:
- Do not route to `question` just because the text is short.
- Do not route to `question` just because the message contains a link.
- Imperatives and open loops can still be `capture` if they are meant as notes to self.
- If the user appears to be thinking aloud for later use, choose `capture`.

Return conservative routing. False negatives are better than false positives.
