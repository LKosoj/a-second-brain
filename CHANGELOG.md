# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and semantic versioning.

## [Unreleased]

## [0.2.0] - 2026-10-26

### Added

- Interactive `codex-tmux` backend with rollout-based answer extraction.
- Reminders: "напомни завтра в 9 позвонить" is parsed without an AI call,
  stored in `vault/.session/reminders.jsonl`, delivered by a ticker in the
  bot, and listed by `/reminders`.
- Morning brief at 08:00 (systemd timer and LaunchAgent): open checkboxes
  from yesterday, this week's ONE Big Thing, pages with open conflicts.
- Ops journal for frontmatter writes (`vault/.session/ops.jsonl` with
  snapshots), CLI `a-second-brain recover <run_id>` / `ops-prune`, and a
  nightly prune to a 30-day window at the end of the scheduled cycle.
- `services/secrets.py`: private keys, bearer tokens, signed URL parameters
  and URL passwords are redacted before external content reaches the vault
  (PLAUD, web archive, documents, YouTube transcripts, forwarded and typed
  messages).
- Freshness lint for compiled pages
  (`skills/vault-health/scripts/freshness_lint.py`); the weekly digest
  report carries its summary.
- Scheduled cycle hardening: restricted CLI flags (claude, codex),
  job-health self-disable after three consecutive failures with a `/menu`
  screen to re-enable, `already_processed` markers for idempotent reruns and
  a VERIFY phase that checks expected artifacts.
- Dependabot, OSV audit, ruff for skill scripts and shellcheck in CI.
- Question and `/do` answers draw a matplotlib chart under
  `attachments/charts/` when the answer has numbers over time or a
  comparison; attachment images referenced in a reply are delivered inline
  (base64) inside one HTML document, never as separate files.
- Import notes are marked `compile_state`/`compile_checked` once their
  compile-enrich pass finishes; a nightly sweep posts stale unmarked ones
  and `run_compiled_import_sweep.py --no-limit` clears the pre-existing
  backlog once.
- Weekly wiki care (`run_compiled_wiki_care.py [--no-limit]`): fills
  missing cross-links and missing pages, answers vault-gap questions and
  runs a Tavily-backed web search for weak compiled topics, gated by its
  own 7-day interval and model-call budget.
- Bot commands `/undo` (removes the last entry of today after confirmation;
  an entry the nightly run has processed is kept), `/find <query>` (keyword
  search over vault notes through `qmd search`, no model call, up to 5
  results with file buttons), `/help` and `/reminders`; `/status` stays an
  alias of `/stats`. Known limits of `/find`: it matches words only (no
  morphology), fresh notes are missing until the qmd index is refreshed, and
  hits whose file name qmd normalizes in its address (for example
  `Заметка Проба #1.md` comes back as `Заметка-Проба-1.md`) are silently
  dropped.
- Editing a sent text message or photo caption in Telegram updates the saved
  entry. If the nightly run has already processed the entry, or the photo had
  no caption, the correction is added to the daily note as a separate entry.
  An entry removed by `/undo` is not revived.
- Reminders can be created from voice messages; `/reminders` has "cancel"
  and "postpone to tomorrow" buttons (a reminder due in the future moves one
  day past its own due date, an overdue one moves to tomorrow).
- Todoist tasks screen in `/menu`, with a "reopen" button after a task is
  closed.
- Overdue Todoist tasks and, on Mondays, "on this day" snippets from
  earlier daily notes in the morning brief.
- `TIMEZONE`: IANA zone of the owner's day (notes, reminders, brief, nightly
  run); systemd timers carry the zone, launchd times are converted.
- `CALENDAR_ICS_URL`: read-only ICS calendar; the morning brief shows the
  day's meetings, the nightly processing gets meetings that have already
  started. Public addresses only; recurring events are supported, `RDATE`
  and Windows time-zone names are not.
- `DAILY_PROCESS_TIME` and `MORNING_BRIEF_TIME` (`HH:MM`); the installers
  render the timers from them.
- Voice without `DEEPGRAM_API_KEY` or after a Deepgram failure is kept with
  status `voice_unrecognized`; the nightly run retries transcription for
  yesterday and today (`services/voice_retry.py`).
- `doctor` checks `TELEGRAM_BOT_TOKEN` via `getMe` (CLI run only; a network
  error is a warning).
- Video, location and contact messages are saved to the daily note; audio
  files and video notes are transcribed like voice; other message types get
  an explicit "not supported" reply.
- Encrypted vault backup streams the tar archive into gpg (no plaintext
  archive on disk) and has a `--verify` check of the newest snapshot.
- Vault backup skips files and directories it cannot read, counts them as
  `unreadable` in the manifest, prints a warning and exits with code 0.
- CI: coverage report and `--cov-fail-under=86`.

### Changed

- CLI subprocesses receive an allowlisted environment instead of the full
  `os.environ`; every `subprocess.run` has a timeout; heavy work in handlers
  runs through `asyncio.to_thread`.
- Telegram MarkdownV2 conversion and screen truncation account for escape
  growth; media-group albums flush reliably; `/do` no longer swallows known
  bot commands.
- PLAUD sync uses a stable content hash, gives up on pending summaries after
  60 days and keeps `first_seen_at` across imports.
- Compiled briefings: rollback refreshes the qmd index; adjudication cache
  key includes the conflict type.
- systemd/launchd units gained `TimeoutStartSec` and `.env` parsing tolerant
  of `export` prefixes; `doctor` warns about unquoted risky values.
- Bot unit uses `KillMode=mixed`, and the next bot start removes tmux
  servers left by a bot that died without closing them (sessions of a live
  process, such as the nightly run, are kept). Re-run
  `scripts/install-systemd-user.sh` to pick up the new unit.
- `DEEPGRAM_API_KEY` is optional.
- `BASE_URL`/`MODEL` are renamed to `OPENAI_BASE_URL`/`OPENAI_MODEL`; the old
  names still work, the new ones win, and `doctor` warns about the old ones.
- The catch-up run hour comes from `DAILY_PROCESS_TIME` unless
  `SCHEDULED_PROCESS_HOUR` is set.
- Summaries of third-party content (links, documents, images, PLAUD) run
  through a restricted CLI mode; text sent to tmux sessions has control
  characters removed.
- Process groups: timed-out helper processes (`uv`, `mcp-cli`, `qmd`, Kimi)
  are stopped together with their children.
- Atomic writes go through shared helpers (`services/atomic_io.py`);
  attachments no longer follow symlinks.
- Bot ignores chats other than the private chat with the owner; users see
  short error texts, details stay in the log.
- `pytest` collects only `tests/` (`testpaths`).
- Daily processing resolves the processing day consistently between the
  runner and the processor.

### Fixed

- With `TIMEZONE` set, both installers pass `TZ=<TIMEZONE>` to every service,
  so skill scripts, the ops log and technical timestamps use the owner's zone.
- `todoist_projects` and the document extractor run in their own process
  group, so a timeout stops their child processes too.
- `/do` transcribes voice, audio files and video notes through the shared
  voice code, including the 20 MB limit.
- `/do` accepts a photo, an album or a document: the files are saved as
  usual and the caption runs as one request on them.
- `/find` recovers hits whose file name qmd normalized in the address.
- An edit that arrives before the session record is written waits for it and
  otherwise becomes a separate correction entry.
- PLAUD sync sends unconfirmed Todoist tasks to Telegram.
- A compiled pass rollback is written to `.session/log.md`
  (`[compile] откат прохода ...`).
- Memory-engine `decay`/`touch` from inside the vault, dry-run writes,
  `access_count` increments and dot-directory skipping.
- tmux stall watchdog for the `claude-tmux` backend; quota-marker detection
  limited to the head of the terminal output.
- Link fetching rejects redirects into private and CGNAT (100.64.0.0/10)
  ranges on every hop and caps download size and duration.
- Context pack reads graph metrics by the keys `analyze.py` actually
  writes; the nightly digest survives a failed takeaways step.
- Storage, frontmatter and source-link edge cases found by the 2026-09-03
  audit.
- PLAUD classification prompt receives the redacted summary and transcript;
  the PLAUD Todoist call uses the allowlisted environment too.
- `recover <run_id>` reports restored, removed and skipped files separately.
- Photo analysis (description and OCR text) is redacted before it reaches
  the daily note and the session log.
- "напомни сегодня ..." with a time already gone asks for a clearer time
  instead of firing on the next ticker run; reminder file I/O and the `/do`
  answer log run through `asyncio.to_thread`.
- Morning brief keeps its other blocks when a compiled page disappears
  while the open-conflicts block is being read.
- `.env` parsing in the installers and `run_with_env.sh` works on macOS's
  bash 3.2 (no negative substring length).
- Nightly processing marks as processed only the entries CAPTURE actually
  read; entries that arrived during EXECUTE or were deferred stay for the
  next run.
- A Todoist timeout is reported as "not confirmed" instead of being retried
  (no duplicate tasks).
- An error in the daily part no longer hides the maintenance sections of the
  nightly digest; a failed `analyze.py` is an error of the vault-health step
  instead of a stale graph.
- Frontmatter migration keeps working on files of another owner (only the
  group is preserved).
- VERIFY lines of the nightly report follow `content_language`.
- A file button in `/menu` answers the callback once.
- The bot no longer blocks on the vault write lock while the nightly run
  holds it, and reminder writes are serialized with a file lock.
- The wait state after `/why` and the brief builder expires after 10
  minutes, and voice, photos and documents sent in it are saved as usual;
  commands (`/...`) reset the wait and run as usual.

## [0.1.0] - 2026-07-29

### Added

- Fresh public distribution with no private Git history.
- Packaged, anonymized vault template and `a-second-brain init`.
- Safe local installer and hardened systemd user-unit templates.
- Telegram capture, processing, retrieval, import, backup, and Todoist
  integration runtime.
- Privacy guidance, automated quality gates, and clean-install tests.
