---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Owner Assignment Policy

Apply these rules to every source that can produce tasks for the assistant owner:
- daily text
- forwarded messages
- imported meeting notes
- PLAUD summaries and transcripts
- captions, OCR, and any other structured or semi-structured input

Owner context:
- The assistant owner is `{OWNER_FULL_NAME}` when provided.
- Create Todoist only when the action is clearly on the assistant owner.
- If unclear, preserve the information in the archive but do not create Todoist.

Assignment rules:
- Evaluate ownership per action item, not only at the source level.
- If one action item is clearly assigned to the owner, keep that task even when other action items in the same source remain ambiguous.
- In personal memos, first-person commitments usually belong to the assistant owner.
- In meetings, calls, forwards, and imported summaries, create Todoist only when the action statement itself makes the owner accountable.
- Shared ownership counts as owner-assigned when the action statement explicitly makes the owner one of the doers.
- Ambiguous mention of a common first name is not enough in group contexts.
- Do not infer ownership from participant lists, surrounding background, or generic team commitments alone.
- If the assistant owner delegates the action to another person and still owns follow-up, control, or delivery risk, create Todoist for the owner as a control task.
- Rewrite delegated work into the owner's controllable next step, for example "Check status from Ivan on the proposal" or "Follow up with Anna on the draft", not "Ivan to prepare the proposal".
- If the action is assigned to another person without any follow-up or control obligation on the owner, do not create Todoist.
- If the action remains unassigned, do not create Todoist.
- If the material is informational or preparatory only, keep it searchable but do not create Todoist.
