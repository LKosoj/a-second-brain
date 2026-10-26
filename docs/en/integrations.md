# Integrations

[Русский](../ru/integrations.md) | [Documentation index](../index.md)

Integrations are enabled by operator configuration. Keep credentials in
`.env`, review each provider's retention terms, and expose only the minimum
permissions required.

## AI CLI backends

Set one backend:

```dotenv
AI_CLI=claude
```

Supported values and common authentication commands:

| Backend | Authentication |
|---|---|
| Claude Code | `claude auth login` |
| Claude Code in tmux (`claude-tmux`) | `claude auth login` |
| Codex CLI | `codex login` |
| Codex in tmux (`codex-tmux`) | `codex login` |
| Qwen Code | `qwen auth qwen-oauth` |
| Gemini CLI | configure a supported Google/Gemini credential |
| Kimi Code | `kimi login` |
| Grok CLI | set `XAI_API_KEY` |
| opencode | `opencode auth login` |

Validate installation and authentication:

```bash
a-second-brain doctor --smoke
```

Agent CLIs receive selected vault context for the requested workflow. Treat the
CLI vendor and its configured model gateway as processors of private data.

Kimi Code runs through its official ACP mode (`kimi acp`). ACP is the protocol
used to exchange requests over the process input and output, so prompt text is
not placed in command-line arguments. Kimi discovers the shared project skills
through `.agents/skills`; no separate Kimi copy is needed.

Grok CLI takes the prompt as a command-line argument, so very long prompts can
hit the argument length limit; every other backend receives the prompt on
standard input.

The `claude-tmux` backend is the same Claude Code, started in its regular
interactive interface inside a detached tmux session instead of headless mode
(`claude -p`). The prompt is pasted through a tmux buffer, and the answer is
read from the session transcript Claude Code writes to
`~/.claude/projects/<project>/<session-id>.jsonl`. It exists as a fallback for
the day headless mode goes away. It requires `tmux`; every run starts a private
tmux server and removes it once the answer arrives.

The `codex-tmux` backend does the same for the interactive Codex interface.
It waits until Codex finishes loading, pastes the prompt through a tmux buffer,
and reads the clean answer from the Codex rollout instead of the terminal pane.

## Deepgram

Deepgram transcribes Telegram voice messages. Configure:

```dotenv
DEEPGRAM_API_KEY=
```

The key is optional. Without it, or when Deepgram fails, the voice message is
kept under `attachments/` with a daily entry marked `voice_unrecognized`
("Не распознано"); the nightly run retries for yesterday and today. Audio is
sent to the configured Deepgram service for transcription.

## Todoist

Todoist actions require:

- `TODOIST_API_KEY`;
- `mcp-cli`;
- `npx`;
- project-local `mcp-config.json`.

The configuration pins the Todoist MCP package version. Task creation is
allowed only by workflows that explicitly grant the action and establish owner
responsibility.

Leave `TODOIST_API_KEY` empty to disable Todoist. `doctor` then reports an
informational result rather than an error.

## Calendar

Set `CALENDAR_ICS_URL` to a private ICS link of your calendar (Google Calendar:
settings of the calendar, "Secret address in iCal format"; Outlook and
Nextcloud: a published calendar). The bot only reads it; nothing is written to
the calendar or the vault.

- The morning brief shows today's meetings (the day follows `TIMEZONE`);
  all-day events come last.
- The nightly processing receives the meetings of the processed day that have
  already started, as context for tasks and CRM. The calendar text is passed to
  the model as data, not as instructions.
- Recurring meetings are supported (`RRULE`, `EXDATE`, moved occurrences);
  `RDATE` is not. Time zones with Windows names (Outlook) are read as your
  `TIMEZONE`, so such meetings can be off by a few hours.
- The link is a secret: anyone with it can read the calendar. Do not publish or
  commit it; the bot never logs or prints it.
- Only public addresses are allowed (protection against requests into your own
  network). A calendar on a home server or `localhost` is skipped, and `doctor`
  warns about it.
- If the link stops working, the brief shows "календарь недоступен" instead of
  the meetings, and the nightly processing runs without the calendar.

Leave `CALENDAR_ICS_URL` empty to disable the calendar.

## QMD

[QMD](https://github.com/tobi/qmd) supplies local vault indexing and retrieval.
This project is tested with `@tobilu/qmd` 2.5.3. Install Node.js, then have an
administrator install the pinned CLI in the system prefix:

```bash
sudo npm install -g @tobilu/qmd@2.5.3
qmd --version
```

The default mode is fully local and does not require the OpenAI-compatible
variables below. When `EMB_MODEL` is empty, the project uses
`Qwen3-Embedding-0.6B`; when `RERANK_MODEL` is empty, QMD uses
`Qwen3-Reranker-0.6B`. The models are downloaded from Hugging Face during the
first embedding or query run and use the CPU by default. To use a remote
embedding provider, set `OPENAI_API_KEY` and `EMB_MODEL`; set `OPENAI_BASE_URL` for a
non-default compatible endpoint (the older `BASE_URL` and `MODEL` still work).
`OPENAI_MODEL` belongs to the recall planner rather
than QMD indexing.

```dotenv
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=
EMB_MODEL=
RERANK_MODEL=
```

Initialize or rebuild the project-local index from a source checkout:

```bash
uv run --frozen --no-dev a-second-brain qmd update
uv run --frozen --no-dev a-second-brain qmd embed
uv run --frozen --no-dev a-second-brain qmd status
```

From an active environment containing the installed wheel:

```bash
a-second-brain qmd update
a-second-brain qmd embed
a-second-brain qmd status
```

The QMD index is derived data, not the source of truth. Vault Markdown remains
canonical. Weekly cleanup is enabled only when `qmd` is discoverable during
systemd unit installation. If QMD is removed later, disable its timer:

```bash
systemctl --user disable --now a-second-brain-qmd-maintenance.timer
```

## PLAUD

Configure recording import with:

```dotenv
PLAUD_REGION=api
PLAUD_BEARER_TOKEN=
```

Supported regions are `api` and `api-euc1`. The optional hourly timer is
enabled only when the token is present.

If Todoist accepted tasks from a recording but returned no ids for them, the
sync sends their count and titles to Telegram ("⚠️ Не подтверждено, проверьте
в Todoist"), the same way as the nightly report.

Removing the token does not disable an already enabled timer:

```bash
systemctl --user disable --now a-second-brain-plaud-sync.timer
```

Imported transcripts may contain personal and business-sensitive material.
They are stored under the private vault and must not enter the public code
repository.

## Web extraction

Optional extraction providers are configured through `TAVILY_API_KEY`,
`JINA_API_KEY`, `ZAI_API_KEY`, and `PROXY_URL`. The runtime can archive the
source and extracted text under the vault's import area.

## Encrypted backups

GPG is an external integration. Only the public recipient key belongs on the
application host. See [Backup and restore](backup-and-restore.md).
