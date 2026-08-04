# A Second Brain

[Русская версия](README.ru.md)

A self-hosted, voice-first personal assistant for Telegram. It captures text
and forwarded messages, voice messages, photos, documents, web pages, and
YouTube links in a private Obsidian-compatible vault. It can import transcripts
and summaries from optional PLAUD recordings, answer questions from the vault,
and create Todoist tasks.

[Full documentation](docs/index.md) covers installation, configuration,
operations, integrations, backups, troubleshooting, architecture, and
development in English and Russian.

The repository contains application code and an anonymized starter-vault
template. Your real `vault/`, `.env`, backups, logs, and runtime state are
ignored by Git and must remain private.

## Requirements

- a dedicated unprivileged Linux account with Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- `jq`
- a Telegram bot token from BotFather
- a Deepgram API key for voice transcription
- one installed and authenticated AI CLI: Claude Code, Codex CLI, Qwen Code,
  or Gemini CLI

Todoist, web extraction, PLAUD, QMD, and encrypted backups are optional and
have additional requirements. See [Integrations](docs/en/integrations.md) and
[Configuration](docs/en/configuration.md).

## Install from a clone

Until a public remote exists, there is no canonical clone URL. This snippet
prompts for the HTTPS or SSH URL shown by the Git host:

```bash
read -r -p "Repository URL: " REPOSITORY_URL
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
