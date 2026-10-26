# Operations

[Русский](../ru/operations.md) | [Documentation index](../index.md)

Run A Second Brain as a dedicated unprivileged Linux account. The supplied
deployment uses systemd user units and never creates a root-owned service.
Commands on this page assume a source checkout and a working systemd user
manager. A wheel-only instance does not contain the supplied unit installer.

## Foreground validation

Before enabling services:

```bash
cd /path/to/a-second-brain
uv run --frozen --no-dev a-second-brain doctor --smoke
uv run --frozen --no-dev a-second-brain run
```

Confirm that `/menu` works from the owner Telegram account, then stop the
foreground process.

## Install systemd user units

Render units without enabling them:

```bash
./scripts/install-systemd-user.sh
```

Review files under `~/.config/systemd/user/`. Enable the standard units only
after review:

```bash
./scripts/install-systemd-user.sh --enable
```

The enable step runs `doctor` first.

## Supplied units

| Unit | Purpose |
|---|---|
| `a-second-brain.service` | Telegram bot |
| `a-second-brain-process.timer` | Daily processing at 21:00 by default (`DAILY_PROCESS_TIME`) |
| `a-second-brain-morning-brief.timer` | Morning brief at 08:00 by default (`MORNING_BRIEF_TIME`) |
| `a-second-brain-plaud-sync.timer` | Hourly PLAUD sync when configured |
| `a-second-brain-qmd-maintenance.timer` | Weekly QMD cleanup when `qmd` exists |
| `a-second-brain-backup-verify.timer` | Weekly backup check, Sunday 04:15 |

To change the times, set `DAILY_PROCESS_TIME` / `MORNING_BRIEF_TIME` (`HH:MM`)
in `.env` and re-run `scripts/install-systemd-user.sh`; it refuses a malformed
value. With `TIMEZONE` set, the two timers carry the zone
(`OnCalendar=*-*-* 21:00:00 Europe/Moscow`), so the time is the owner's whatever
the server zone is; re-run the installer after changing `TIMEZONE`. Before classification the nightly run also retries transcription of
voice entries marked "Не распознано" for yesterday and today.

The daily processing service runs an encrypted backup preflight before
write-heavy processing. With no GPG recipient configured, that preflight
reports a skip and exits successfully.

## Telegram commands

| Command | Action |
|---|---|
| `/do` | Run a free-form request (text, voice, audio file, video note, photo or document) |
| `/why` | Explain why a compiled page states a claim |
| `/find` | Search notes by keywords, no model call |
| `/undo` | Remove the last entry of today |
| `/reminders` | List pending reminders |
| `/process`, `/process_full` | Preview or run the daily processing |
| `/menu` | Open the dashboard |
| `/stats` | Capture statistics (`/status` is an old alias) |
| `/files` | Browse vault files |
| `/help` | Show the command list (`/start` shows the same text) |

`/find <query>` runs `qmd search` (keyword search, no model) and shows up to
five results with a path and a snippet; the "📄 N" buttons send the file. Without
a query the bot waits for the text for 10 minutes; `-` or a repeated `/find`
cancels, and commands (`/...`) reset the wait and run as usual. `qmd` must be in `PATH`, otherwise the bot answers that search is
unavailable. The query is not written to the session or the daily note. Hidden
directories (such as `.session`) and `skills/` are never returned. Limits: the
search matches words (no morphology or meaning), depends on the qmd index (a
fresh note may not be found yet). qmd normalizes file names in its addresses
(`Заметка Проба #1.md` comes back as `Заметка-Проба-1.md`); the bot recovers
the name with qmd's own rule. If one normalized name fits two files of the
same collection, the hit is skipped rather than guessed.

`/undo` shows the newest entry of today and asks for confirmation. It removes
only the daily entry (not attachments or compiled pages), only for the current
day of the owner (`TIMEZONE`, see [Configuration](configuration.md)), and not
for an entry the nightly run has already processed.

Editing a plain text message or a photo caption in Telegram updates the saved
entry. If the nightly run has already processed the entry, or the photo had
no caption, the correction is added to the daily note as a separate entry.
If the edit arrives before the bot has logged the message to the session, the
bot waits up to 5 seconds for that record; if it does not appear but the
message is already in the daily note, the correction is also added as a
separate entry. Commands ("/...") are not entries, and an edit does not revive an entry
removed with `/undo`.

A photo or document sent in `/do` mode is saved as usual (attachment and daily
entry), and its caption runs as a request on that file. Without a caption the
bot just reviews the file. The caption of a forwarded message is never treated
as a request. An album is saved as one entry, and the request runs once over
all of its photos.

`/reminders` lists pending reminders. Create one with a phrase such as
"напомни завтра в 9 позвонить".

## Service control

```bash
systemctl --user status a-second-brain.service
systemctl --user restart a-second-brain.service
systemctl --user stop a-second-brain.service
systemctl --user start a-second-brain.service
```

The bot unit uses `KillMode=mixed`: on stop or restart the bot gets SIGTERM
first and closes its tmux sessions, then systemd kills anything left in the
unit. A running unit keeps its old `KillMode` until you re-run
`scripts/install-systemd-user.sh` (it reinstalls the template) and
`systemctl --user daemon-reload`. Note that `mixed` also terminates the
bot's child processes on stop or restart, so any CLI run in progress is
interrupted. If the bot died without closing them (crash, SIGKILL), the next
bot start removes the leftover tmux servers of dead owners; sessions of a live
process, such as the nightly run, are left alone.

Timer inspection:

```bash
systemctl --user list-timers 'a-second-brain-*'
systemctl --user status a-second-brain-process.timer
```

## Logs

The application writes to stdout and stderr. Under systemd, inspect the
journal:

```bash
journalctl --user -u a-second-brain.service -n 200 --no-pager
journalctl --user -u a-second-brain.service -f
journalctl --user -u a-second-brain-process.service --since today
```

Do not paste logs publicly before checking them for captured text, paths,
Telegram identifiers, and provider errors.

## Update a source checkout

Stop write-heavy work before a significant update:

```bash
for unit in \
  a-second-brain-process.timer \
  a-second-brain-morning-brief.timer \
  a-second-brain-plaud-sync.timer \
  a-second-brain-qmd-maintenance.timer \
  a-second-brain.service \
  a-second-brain-process.service \
  a-second-brain-morning-brief.service \
  a-second-brain-plaud-sync.service \
  a-second-brain-qmd-maintenance.service
do
  systemctl --user stop "$unit" 2>/dev/null || true
done
git pull --ff-only
uv sync --frozen --no-dev
uv run --frozen --no-dev a-second-brain doctor
./scripts/install-systemd-user.sh --enable
```

If deployment templates changed, the installer performs `daemon-reload`.
Review release notes and create a verified backup before migrations.

## Run maintenance manually

```bash
uv run --frozen --no-dev python -m d_brain.run_daily_process --mode scheduled
uv run --frozen --no-dev python -m d_brain.run_plaud_sync
uv run --frozen --no-dev a-second-brain qmd cleanup
uv run --frozen --no-dev python -m d_brain.run_vault_backup
```

Manual commands use the same `.env` and project working directory as the
services.

To undo one run's vault writes, use `a-second-brain recover <run_id>`; to trim
the ops journal by hand, `a-second-brain ops-prune` (see `docs/en/cli.md`).
Stop the bot and make sure no nightly process is running before either.

## Run after logout

User services normally stop when the account has no session. An administrator
may enable lingering for the dedicated account:

```bash
sudo loginctl enable-linger <service-account>
```

This is a host-level policy decision and is intentionally outside the project
installer.
