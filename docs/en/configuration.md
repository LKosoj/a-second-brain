# Configuration

[Русский](../ru/configuration.md) | [Documentation index](../index.md)

Runtime configuration is read from `.env` in the instance directory.
`a-second-brain init` creates it with mode `0600`. Never commit this file or
paste its values into issues, logs, or documentation.

## Required settings

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from BotFather |
| `OWNER_TELEGRAM_ID` | Positive numeric Telegram user ID allowed to use the bot |

`DEEPGRAM_API_KEY` is optional and enables voice transcription. Without it, or
when Deepgram fails, the bot still saves the recording to `attachments/` and a
daily entry marked "Не распознано"; the nightly run transcribes it once the key
works (for today and yesterday).

`AI_CLI` defaults to `claude`, but the selected command must be installed and
authenticated.

## Schedule

| Variable | Purpose |
|---|---|
| `MORNING_BRIEF_TIME` | Morning brief time, `HH:MM` 24h, default `08:00` |
| `DAILY_PROCESS_TIME` | Nightly processing time, `HH:MM` 24h, default `21:00` |
| `SCHEDULED_PROCESS_HOUR` | Optional hour `0`-`23` after which a scheduled run counts as on time; default is the hour of `DAILY_PROCESS_TIME`. An invalid value falls back to the default |
| `TIMEZONE` | IANA zone of the owner's day, for example `Europe/Moscow`; empty uses the system zone |
| `CALENDAR_ICS_URL` | Private ICS link of your calendar (read-only); empty disables the calendar. See [Integrations](integrations.md#calendar) |

The installers (`scripts/install-systemd-user.sh`,
`scripts/install-launchd-user.sh`) read these from `.env` and stop on a
malformed value. Re-run the installer after changing them. Pick an evening
time: a run before `DAILY_PROCESS_TIME` is treated as a late catch-up for the
previous day.

`TIMEZONE` decides which day an entry, a brief or the nightly run belongs to
(an entry that arrives at 22:30 UTC lands in the next Moscow day's daily note)
and what "remind me at 9" means. The times above are read in that zone. An
unknown name stops the bot at start and `doctor` reports it. Leave it empty to
keep the system zone. The systemd installer writes the zone into the two
timers; the maintenance timers (QMD, backup check, PLAUD) stay on the system
zone. Both installers (`install-systemd-user.sh`, `install-launchd-user.sh`)
pass the zone to every service as `TZ=<TIMEZONE>`, so skill scripts, the ops
log and technical timestamps also count time in the owner's zone. A `TZ` set
explicitly in `.env` takes precedence over the one derived from `TIMEZONE`.
Re-run the installer after changing `TIMEZONE`. Notes and reminders already written are not
converted when the zone changes, and `/undo` may not find an entry from the
previous day around the switch.

## Core behavior

| Variable | Default | Purpose |
|---|---:|---|
| `AI_CLI` | `claude` | `claude`, `claude-tmux`, `codex`, `codex-tmux`, `qwen`, `gemini`, `kimi`, `grok`, or `opencode` |
| `CONTENT_LANGUAGE` | `ru` | Language used for saved and generated content |
| `OWNER_FULL_NAME` | empty | Owner matching in task-assignment prompts |
| `VAULT_PATH` | `./vault` | Private Obsidian-compatible vault |

Keep `VAULT_PATH` aligned with `memory_root` in `vault-manifest.json`. The
standard layout uses `./vault`.

## Todoist

| Variable | Default | Purpose |
|---|---:|---|
| `TODOIST_API_KEY` | empty | Enables Todoist task actions |

Todoist also requires `mcp-cli`, `npx`, and the project-local
`mcp-config.json`. When the key is empty, `doctor` treats Todoist as disabled.

## Web extraction

The application tries configured extraction providers when ordinary HTTP
retrieval is insufficient.

| Variable | Purpose |
|---|---|
| `TAVILY_API_KEY` | Tavily Extract |
| `JINA_API_KEY` | Jina Reader |
| `ZAI_API_KEY` | Z.AI Reader fallback |
| `PROXY_URL` | Optional compatible proxy reader |

All are optional. Configure only providers whose privacy and retention terms
you accept.

## PLAUD

| Variable | Default | Purpose |
|---|---:|---|
| `PLAUD_BEARER_TOKEN` | empty | Enables PLAUD recording import |
| `PLAUD_REGION` | `api` | `api` or `api-euc1` |

The PLAUD systemd timer is enabled only when the token is present during unit
installation.

## Search and compatible model gateway

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | API key for the compatible gateway |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL; the older name `BASE_URL` still works, the new one wins if both are set |
| `CLASSIFICATION_MODEL` | Decisions API model for message routing and memory-search decisions; takes priority over `OPENAI_MODEL` |
| `OPENAI_MODEL` | Chat API model for the same decisions when `CLASSIFICATION_MODEL` is empty; both empty uses CLI. The older name `MODEL` still works |
| `EMB_MODEL` | Remote QMD embedding model; empty uses local `Qwen3-Embedding-0.6B` |
| `RERANK_MODEL` | QMD reranking model; empty uses local `Qwen3-Reranker-0.6B` |

Empty `EMB_MODEL` and `RERANK_MODEL` values keep the mode fully local and do
not require `OPENAI_API_KEY` or `OPENAI_BASE_URL`. The local models are downloaded
from Hugging Face on first use and run on the CPU by default. Do not assume
that a remote OpenAI-compatible endpoint has the same privacy properties as
OpenAI.

## Encrypted backups

| Variable | Default | Purpose |
|---|---:|---|
| `VAULT_BACKUP_DIR` | `./.vault-backups` | Encrypted snapshot directory |
| `VAULT_BACKUP_GPG_RECIPIENT` | empty | GPG recipient; empty disables snapshots |
| `VAULT_BACKUP_RETENTION` | `14` | Number of snapshots retained |

The backup directory must remain outside `VAULT_PATH`. Keep the private
decryption key off the application host. See
[Backup and restore](backup-and-restore.md).

## Gemini authentication variables

Gemini authentication may use `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS`, or `GOOGLE_GENAI_USE_VERTEXAI`. The other AI
CLIs use their own login commands.

## Light models for individual CLI requests

`CliRunner.run(..., light=True)` tries a light model for a read-only request.
Claude uses `haiku`, and Gemini uses `flash-lite`. Codex, Grok, Kimi and
OpenCode use their CLI catalogs; Qwen uses its configured `modelProviders`.
Known light families are preferred, with newer numeric versions selected
within a tier. A catalog entry does not guarantee account access.

If the selected model is rejected, the runner retries once without a model
override, preserving the CLI's configured default. Missing catalogs or no
matching light model also use the default. Network, authentication, quota
and timeout failures do not trigger another model. Both attempts share the
request timeout. Selection and fallback are recorded in the application log.
The tmux backends support the same selection. Existing callers keep their
normal model until they explicitly request `light=True`.
The task list's detail arrow requests light models for both CLI-based retrieval
planning and the final answer. Other questions and actions keep their normal model.

## Validation

Run diagnostics after every configuration change:

```bash
a-second-brain doctor
```

The command reports whether a value is present, but never prints secret
values.
