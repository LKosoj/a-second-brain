# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and semantic versioning.

## [Unreleased]

### Added

- Reminders: "напомни завтра в 9 позвонить" is parsed without an AI call,
  stored in `vault/.session/reminders.jsonl`, delivered by a ticker in the
  bot, and listed by `/reminders`.
- Morning brief at 08:00 (systemd timer and LaunchAgent): open checkboxes
  from yesterday, this week's ONE Big Thing, pages with open conflicts.
- "👍 Сохранить" button under `/do` and `/why` answers writes a card to
  `vault/answers/` and a line to `vault/.session/log.md`.
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
- systemd/launchd units gained `TimeoutStartSec`, `KillMode=process` for the
  bot, `.env` parsing tolerant of `export` prefixes; `doctor` warns about
  unquoted risky values.
- Daily processing resolves the processing day consistently between the
  runner and the processor.

### Fixed

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
- `recover <run_id>` reports restored, removed and skipped files separately;
  "👍 Сохранить" cannot overwrite a card written concurrently.
- Photo analysis (description and OCR text) is redacted before it reaches
  the daily note and the session log.
- "напомни сегодня ..." with a time already gone asks for a clearer time
  instead of firing on the next ticker run; reminder file I/O and the `/do`
  answer log run through `asyncio.to_thread`.
- Morning brief keeps its other blocks when a compiled page disappears
  while the open-conflicts block is being read.
- `.env` parsing in the installers and `run_with_env.sh` works on macOS's
  bash 3.2 (no negative substring length).

## [0.1.0] - 2026-07-29

### Added

- Fresh public distribution with no private Git history.
- Packaged, anonymized vault template and `a-second-brain init`.
- Safe local installer and hardened systemd user-unit templates.
- Telegram capture, processing, retrieval, import, backup, and Todoist
  integration runtime.
- Privacy guidance, automated quality gates, and clean-install tests.
