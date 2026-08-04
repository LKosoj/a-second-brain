# Public distribution design

## Goal

Create a public, installable Agent Second Brain repository without exposing the
owner's vault, runtime state, credentials, or private Git history.

The existing private checkout remains private and continues to run unchanged
during this work.

## Repository boundary

The public distribution is a new repository with a fresh history. It contains
application code, tests, documentation, deployment templates, and an anonymous
vault template. It must never be populated by copying the private `.git`
directory or by exporting the complete private `HEAD`.

The generated `vault/` is runtime data and is ignored as a whole. Its initial
content comes from package resources so source installs and Python
distributions use the same template.

## Installation flow

1. Clone the public repository.
2. Run `./install.sh`.
3. The installer syncs locked Python dependencies and runs
   `a-second-brain init`.
4. `init` creates `vault/`, `.env`, `vault-manifest.json`, and
   `mcp-config.json` without overwriting existing files.
5. The owner edits `.env`, runs the doctor, and optionally installs user-level
   systemd units from deployment templates.

The installer does not collect or print secrets, configure a Git push token, or
start services before configuration is complete.

## Template contents

The template includes the first-party runtime instructions and scripts needed
by the processing, retrieval, graph, and memory workflows, plus the optional
skills explicitly selected for the public distribution. Personal profiles,
daily notes, goals, imports, compiled notes, summaries, sessions, locks, caches,
photos, and generated classifications are excluded.

Locally private skills are not bundled.

## Deployment

Deployment templates are user-level systemd units. They contain placeholders
for the checkout and `uv` paths and do not declare `User=root`. Installation of
units is an explicit second step and does not affect the existing live service.

## Verification

The repository is ready when:

- banned private and runtime paths are absent from Git;
- personal markers and credential scanners report no findings requiring action;
- the wheel and sdist contain the anonymous template and CLI entry point;
- initialization succeeds in an empty temporary directory and refuses to
  overwrite existing data;
- tests, Ruff, mypy, shell syntax checks, and package builds pass;
- the fresh Git history contains only the reviewed public tree.
