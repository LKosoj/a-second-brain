# Doctor command and bilingual documentation design

## Goals

- Provide `a-second-brain doctor [PROJECT_DIR] [--smoke]` from both a source
  checkout and an installed wheel.
- Keep one implementation of diagnostics.
- Preserve `scripts/doctor.sh` as a compatibility wrapper.
- Publish complete, mirrored English and Russian operator documentation.

## Doctor architecture

`src/d_brain/doctor.py` owns all checks and output. The public CLI delegates to
it. The compatibility shell script delegates back to the CLI and contains no
diagnostic logic.

The command checks:

- `.env` existence and restrictive permissions;
- required and optional environment settings without printing their values;
- a valid positive `OWNER_TELEGRAM_ID`;
- vault, manifest, and MCP configuration;
- required commands and the selected AI CLI;
- AI CLI authentication;
- conditional Todoist, backup, QMD, and PLAUD prerequisites;
- an optional one-shot AI request when `--smoke` is supplied.

The command returns `0` when required checks pass and `1` when any required
check fails. Warnings and informational findings do not fail the command.
Argument errors retain the standard argparse exit code `2`.

## Documentation structure

`docs/index.md` is the language selector. `docs/en/` and `docs/ru/` contain the
same set of pages:

- getting started;
- configuration;
- CLI reference;
- operations;
- integrations;
- backup and restore;
- troubleshooting;
- architecture;
- development.

Every page links to its counterpart. Both README files link to the
documentation index. Existing VPS and backup pages become compatibility
pointers; the detailed control-plane reference remains an advanced source.

## Verification

- Unit tests cover passing and failing doctor checks, secret-safe output,
  smoke behavior, and CLI routing.
- Documentation tests require mirrored page names, counterpart links, command
  coverage, and README navigation.
- Ruff, mypy, pytest, ShellCheck, package build, and a wheel-only clean-room
  doctor run must pass.
- A reader with no project-development context must be able to answer common
  installation, configuration, operation, and recovery questions from the
  documentation.
