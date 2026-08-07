# A Second Brain

[Русская версия](README.ru.md)

A self-hosted, voice-first personal assistant for Telegram. Send it a thought,
a question, a voice message, a photo, a document, or a link. It keeps the
source material in a private Obsidian-compatible vault, turns useful input into
notes and tasks, and can answer later from what you saved.

It also synthesizes. A nightly pass compiles scattered notes into maintained
pages about your projects, people, and decisions, keeps every claim tied to the
note it came from, and brings genuine contradictions to you instead of quietly
picking one.

[Full documentation](docs/index.md) covers installation, configuration,
memory and search, operations, integrations, backups, troubleshooting,
architecture, and development in English and Russian.

The repository contains application code and an anonymized starter-vault
template. Your real `vault/`, `.env`, backups, logs, and runtime state are
ignored by Git and must remain private.

## Why it exists

Capturing a thought is easy. Keeping notes organized enough to find that
thought next month is the hard part. A Second Brain reduces the maintenance:
Telegram is the inbox, Markdown is the durable record, and the agent handles
classification, retrieval, linking, and scheduled review.

Finding the note is only half of it. What you know about a project is usually
spread across a dozen entries written months apart, some of which contradict
each other. So the system maintains a synthesized layer above the raw notes: a
status or history question starts from one current page instead of a dozen
fragments, with the sources behind each claim still attached. The notes stay
underneath, and a question about an exact date, name, or identifier still goes
to them first.

The system does not hide your knowledge in a proprietary database. The source
of truth is a directory of readable Markdown files and attachments that can be
opened in Obsidian or managed with ordinary filesystem tools.

## What it does

- Captures text, forwarded messages, voice, photos, albums, and documents from
  one authorized Telegram account.
- Transcribes voice with Deepgram and preserves raw daily entries before later
  processing.
- Archives supported web pages, YouTube material, uploaded documents, and
  optional PLAUD transcripts with their source context.
- Distinguishes likely questions from notes. Questions are answered immediately;
  notes are kept for the daily processing cycle.
- Turns classified entries into knowledge cards, business or project updates,
  and optional Todoist tasks during the full processing cycle.
- Builds daily, weekly, monthly, and yearly reviews.
- Compiles scattered notes into maintained pages for projects, people, topics,
  decisions, meetings, and concepts, keeping each claim tied to its source note.
- Re-checks those pages as they age. A contradiction settled by date, or one
  that only looks like a contradiction because the two claims describe
  different situations, is resolved without asking; a real factual conflict
  waits for you in a decisions queue.
- Sends a daily digest of what needs your decision, what changed, and which
  long-forgotten page is worth another look.
- Ages memory metadata over time, promotes notes when they are used, and keeps
  old material available for deep recall instead of deleting it.
- Checks the note graph for broken links, weak metadata, and isolated notes, and
  maintains Maps of Content where the vault structure needs them.

## What you send and what happens

| Input | Immediate result | Later use |
|---|---|---|
| Voice message | Deepgram transcript saved to the daily note | Classified during processing and available to search |
| Text thought | Original text saved to the daily note | May become a task, knowledge card, or context update |
| Text question | Answer built from goals, memory, briefings, and retrieved notes | A longer reusable answer may be filed back into the searchable vault |
| Forwarded message or link | Source information and extracted content are preserved when available | Included in recall and daily review |
| Photo or album | Image saved under attachments with its caption and best-effort AI description | Can be linked to related daily or project context |
| Document | Original upload is staged first, then text is extracted and archived | Searchable import plus a daily reference |
| PLAUD recording | Optional sync imports transcript and summary | Recent actions assigned to the owner can become Todoist tasks |

## From inbox to organized notes

New captures are written first, so a later model or integration failure does
not discard the original input. There are then three ways the system uses it:

1. A direct question is routed to the most useful context: current goals and
   long-term memory, compiled briefings, note retrieval, or exact lookup.
2. `/process` runs a quick preview of today's entries without executing the
   write-heavy steps.
3. The scheduled cycle at 21:00, or `/process_full`, runs three isolated phases:
   capture and classification, execution, then reflection and maintenance.

Periodic cycles add weekly, monthly, and yearly reviews. Optional timers refresh
the local search index and synchronize PLAUD independently of the bot process.

## Memory and search at a glance

The assistant does not load the whole vault for every request. It combines four
layers:

- a small, bounded core context with long-term memory, current goals, recent
  daily notes, and the previous handoff;
- compiled pages, which are synthesized from many notes for fast status and
  history questions and keep the sources behind each claim;
- QMD semantic search, meaning search by idea rather than exact wording;
- exact text search for identifiers, paths, dates, and other literal strings.

Each note can carry a memory tier and a relevance score. Recent or repeatedly
used notes rank higher in normal recall. When QMD is installed and indexed,
deep recall includes cold and archived notes; an exceptionally strong match can
still appear in normal recall. Opening a note through `qmd get`, or explicitly
touching a directly read file, promotes it one tier at a time. Decay never
deletes it.

The complete model, including note organization, retrieval order, tier
thresholds, daily-entry tracking, correction history, QMD commands, and what is
or is not automatic, is in [Memory, notes, and search](docs/en/memory-and-search.md).

## Vault layout

```text
vault/
├── MEMORY.md          # Curated durable context
├── daily/             # Raw chronological inbox
├── goals/             # Vision, yearly, monthly, and weekly focus
├── thoughts/          # Reusable knowledge cards and reflections
├── business/          # CRM, network, and event context
├── projects/          # Clients, leads, and active projects
├── imports/           # Archived documents, web pages, YouTube, and PLAUD
├── compiled/          # Synthesized pages with sources; rebuildable from notes
├── summaries/         # Periodic reports
├── MOC/               # Maps of Content for navigation
└── attachments/       # Uploaded images and other binary files
```

The private vault also contains rebuildable indexes, session state, rules, and
Obsidian settings. `vault-manifest.json` defines which paths are user content
and which paths are runtime infrastructure.

## Everyday Telegram commands

| Command | Action |
|---|---|
| `/do` | Run a free-form request against the vault and optional Todoist access |
| `/why` | Explain why a compiled page states a claim, with its sources |
| `/process` | Preview today's classification without write-heavy execution |
| `/process_full` | Run the full daily cycle now |
| `/menu` | Open the dashboard: digest, decisions queue, brief builder, weekly summary |
| `/stats` | Show capture statistics |
| `/files` | Browse and download allowed vault files |

See the [CLI reference](docs/en/cli.md) for installation and maintenance
commands and the bot help screen for the complete Telegram command list.

## Requirements

- a dedicated unprivileged Linux account with Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- `jq`
- a Telegram bot token from BotFather
- a Deepgram API key for voice transcription
- one installed and authenticated AI CLI: Claude Code, Codex CLI, Qwen Code,
  Gemini CLI, or Kimi Code

Todoist, web extraction, PLAUD, QMD, and encrypted backups are optional and
have additional requirements. See [Integrations](docs/en/integrations.md) and
[Configuration](docs/en/configuration.md).

## Install from a clone

Clone the public repository, or set `REPOSITORY_URL` to your own fork:

```bash
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/LKosoj/a-second-brain.git}"
git clone "$REPOSITORY_URL" a-second-brain
cd a-second-brain
./install.sh
```

Edit `.env` and set `TELEGRAM_BOT_TOKEN`, `DEEPGRAM_API_KEY`, and
`OWNER_TELEGRAM_ID`. Set `AI_CLI` only when using a backend other than the
default `claude`. Only the Telegram account identified by
`OWNER_TELEGRAM_ID` is accepted. Then run:

```bash
uv run --frozen --no-dev a-second-brain doctor
uv run --frozen --no-dev a-second-brain run
```

`install.sh` refuses to run as root, installs locked dependencies, and creates
the private vault only when `vault/` does not already exist.

## Install from a wheel

Download the wheel from the Git host's Releases page, or build it in a source
checkout with `uv build`. Put one wheel in the current directory. In an
activated Python 3.12+ environment with `pip`, run:

```bash
python -m pip install ./a_second_brain-*.whl
INSTANCE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/a-second-brain"
a-second-brain init "$INSTANCE_DIR"
cd "$INSTANCE_DIR"
```

`init` creates a generic vault, `.env.example`, a mode-`0600` `.env`,
`mcp-config.json`, `vault-manifest.json`, and a protective `.gitignore`. It
preserves existing project files and refuses to overwrite an existing vault.

Edit `.env` as described above, then run:

```bash
a-second-brain doctor
a-second-brain run
```

## User service from a source checkout

The supplied systemd installer is part of the source checkout and is not
included in a wheel-created instance. It requires a working systemd user
manager.

To render systemd user units without enabling them:

```bash
./scripts/install-systemd-user.sh
```

Review the generated files under
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`, then enable the bot and
scheduled processing:

```bash
./scripts/install-systemd-user.sh --enable
```

`--enable` runs `doctor` before enabling any unit. If diagnostics pass, it
enables and starts the bot and daily processing. It enables and starts the
PLAUD timer when `PLAUD_BEARER_TOKEN` is non-empty and the QMD timer when `qmd`
is on `PATH`. Removing either prerequisite later does not disable an already
enabled timer; see
[Integrations](docs/en/integrations.md). No root-owned system service is
installed.

## Privacy boundary

- In a source checkout, the generated `vault/` is inside the project directory
  but ignored by Git; it must remain private.
- Never copy a populated vault, `.env`, logs, backups, or runtime state into
  this repository.
- Treat `.gitignore` as a publication safeguard, not access control or
  encryption; restrict filesystem access to the service account.
- Self-hosting does not make every operation local. Telegram transports bot
  messages and uploads; voice audio is sent to Deepgram. The configured AI CLI
  runs with access to the private vault and may send vault context to its model
  provider. Todoist exchanges task and project data; web extractors receive
  source URLs and page content; YouTube receives retrieval requests; PLAUD sync
  fetches recording metadata and transcripts; remote QMD sends note chunks to
  the configured embedding or reranking endpoint.
- Run a secret scanner before every public release.
- Revoke a credential immediately if it is ever committed; deleting it in a
  later commit is not enough.

See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## Development

The complete required gate list is in
[Development](docs/en/development.md#quality-gates).

```bash
uv sync --frozen --group dev
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest -q
uv build
```

Workflow contracts and routing live in
`src/d_brain/control_plane/registry.py` and
`src/d_brain/control_plane/router.py`. Runtime orchestration is implemented in
`src/d_brain/services/`, including `daily_workflow.py`; the bundled vault files
are runtime prompts, policies, and starter content. See
[ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/control-plane.md](docs/control-plane.md).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
