# Deploying on macOS

[Русский](../ru/macos.md) | [Documentation index](../index.md)

The repository ships Linux-first (`scripts/install-systemd-user.sh` +
`deploy/*.service.in`). On a Mac, the same schedules run as user
LaunchAgents — one plist per service, all in
`~/Library/LaunchAgents/com.second-brain.*.plist`.

## Prerequisites

| Tool | Install |
| --- | --- |
| Python 3.12 | `brew install python@3.12` (Apple Silicon or Intel) |
| `uv` | `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `opencode` (or any other AI CLI) | `brew install anomalyco/tap/opencode`, then `opencode auth login` |
| `git` | `brew install git` if not already present |

## One-time setup

```bash
# 1. Clone and install Python deps
mkdir -p ~/second-brain ~/second-brain-data
git clone https://github.com/LKosoj/a-second-brain.git ~/second-brain/repo
cd ~/second-brain/repo
uv sync --frozen --no-dev

# 2. Create .env (chmod 600 is set by init, but be explicit if you hand-craft it)
cp .env.example .env
chmod 600 .env
$EDITOR .env
# Required: TELEGRAM_BOT_TOKEN, DEEPGRAM_API_KEY, OWNER_TELEGRAM_ID
# Pick your brain: AI_CLI=opencode (default in .env.example is claude)
# VAULT_PATH=$HOME/second-brain/repo/vault
# CONTENT_LANGUAGE=ru

# 3. Smoke-test
uv run --frozen --no-dev a-second-brain doctor ~/second-brain/repo
uv run --frozen --no-dev a-second-brain run
# Ctrl-C once the bot reports "Starting bot polling"

# 4. Install LaunchAgents
bash scripts/install-launchd-user.sh --enable

# 5. Disable Mac sleep so polling never stops
sudo pmset -a sleep 0 disksleep 0 displaysleep 0
```

The `--enable` step renders four plists from `deploy/*.plist.in`:

| Label | Schedule | Command |
| --- | --- | --- |
| `com.second-brain.bot` | `RunAtLoad` + `KeepAlive` | `a-second-brain run` |
| `com.second-brain.process` | daily 21:00 | `python -m d_brain.run_daily_process --mode scheduled` |
| `com.second-brain.plaud-sync` | hourly (only if `PLAUD_BEARER_TOKEN` is set) | `python -m d_brain.run_plaud_sync` |
| `com.second-brain.qmd-maintenance` | weekly Sun 03:30 (only if `qmd` is on `PATH`) | `a-second-brain qmd cleanup` |

## Inspecting and managing

```bash
launchctl list | grep second-brain            # which agents are loaded
launchctl start com.second-brain.bot     # force a run
launchctl stop com.second-brain.bot      # stop the long-lived bot
launchctl unload ~/Library/LaunchAgents/com.second-brain.bot.plist
launchctl load -w ~/Library/LaunchAgents/com.second-brain.bot.plist

# Logs (rotated manually if needed; macOS does not auto-rotate user logs)
ls ~/Library/Logs/com.second-brain/
tail -f ~/Library/Logs/com.second-brain/bot.err.log

# Trigger the daily job now (for testing)
launchctl start com.second-brain.process
```

## Updating

```bash
cd ~/second-brain/repo
git pull --ff-only
uv sync --frozen --no-dev
bash scripts/install-launchd-user.sh --enable
```

`--enable` is idempotent: it unloads and re-loads each agent, picking up
the new plist content.

## Uninstalling

```bash
bash scripts/install-launchd-user.sh --uninstall
sudo pmset -a sleep 1 disksleep 1 displaysleep 1   # restore defaults
```

## Differences from systemd

| systemd | launchd |
| --- | --- |
| `EnvironmentFile=@PROJECT_DIR@/.env` | `scripts/lib/run_with_env.sh` (sourced by the plist's `ProgramArguments[0]`) |
| `Restart=on-failure` + `RestartSec=10` | `KeepAlive.Crashed: true` + `ThrottleInterval: 10` |
| `OnCalendar=*-*-* 21:00:00` | `StartCalendarInterval { Hour: 21, Minute: 0 }` |
| `OnCalendar=hourly` | `StartInterval: 3600` |
| `OnCalendar=Sun 03:30:00` | `StartCalendarInterval { Weekday: 0, Hour: 3, Minute: 30 }` |
| `journalctl -u a-second-brain` | `~/Library/Logs/com.second-brain/*.log` |

The plists reference `run_with_env.sh` so they pick up `.env` the same way
`EnvironmentFile` does on Linux. Logs land in `~/Library/Logs/` per
Apple's convention instead of systemd's journal.