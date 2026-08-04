# Control-Plane Security Profile Template

Template for risky or externally sourced integrations.

Use this for workflows that ingest or act on:
- email
- calendar
- browser/web content
- chat transcripts from third parties
- external documents with embedded instructions
- any source that can contain hostile or manipulative text

## Profile Fields

### Workflow

- `workflow_name`:
- `owner`:
- `runtime_entrypoint`:

### Trust Boundary

- `untrusted_input_sources`:
- `trusted_internal_state`:
- `parsed_artifacts`:

### Allowed Actions

- `read_actions`:
- `write_actions`:
- `external_side_effects`:

### Forbidden Actions

- `forbidden_shell_interpolation`:
- `forbidden_instruction_sources`:
- `forbidden_implicit_writes`:

### Fallback And Escalation

- `read_only_fallback`:
- `write_escalation_rule`:
- `human_confirmation_required_for`:

### Auditability

- `audit_trail_location`:
- `logged_decisions`:
- `rollback_or_repair_path`:

## Mandatory Rules

Every risky integration must satisfy these rules:

1. External text is untrusted input.
2. Instructions inside external content are never executable instructions for the agent by default.
3. Raw external content is never interpolated directly into shell commands.
4. Allowed actions are explicitly allowlisted.
5. A read-only fallback path is documented.
6. Write operations have a capability boundary and audit trail.

## Minimal Example

```yaml
workflow_name: integration.email.readonly
owner: inbox
runtime_entrypoint: d_brain.services.inbox.EmailReader.fetch_messages

untrusted_input_sources:
  - email.subject
  - email.body
  - attachments.text

trusted_internal_state:
  - vault/MEMORY.md
  - vault/goals/*
  - Todoist project map

parsed_artifacts:
  - extracted_summary
  - candidate_tasks

read_actions:
  - fetch messages
  - summarize content
  - propose tasks

write_actions:
  - append daily note

external_side_effects:
  - none

forbidden_shell_interpolation: true
forbidden_instruction_sources:
  - email body
  - attachment text
forbidden_implicit_writes:
  - CRM updates
  - calendar changes
  - outgoing email

read_only_fallback: summarize email into daily without any external side effects
write_escalation_rule: explicit workflow approval before any write outside daily/summaries
human_confirmation_required_for:
  - send email
  - update calendar
  - modify CRM

audit_trail_location: vault/summaries/integrations/
logged_decisions:
  - why a write was allowed
  - what source triggered it
rollback_or_repair_path: remove generated artifact and re-run in read-only mode
```

## Usage

When adding a new risky integration:
1. Copy this template.
2. Fill every field.
3. Link it from the workflow entry or the integration doc.
4. Keep the execution logic in Python services, not in free-form prompt prose.
