# Development

[Русский](../ru/development.md) | [Documentation index](../index.md)

Development happens against the public source tree and anonymized fixtures.
Never use a populated personal vault as test data.

## Environment

```bash
uv sync --frozen --group dev
```

Python 3.12 is the minimum supported version. Runtime dependencies are locked
in `uv.lock`; ordinary installation excludes the development group. Install
`shellcheck` separately on the host before running all gates.

## Quality gates

Run before proposing a change:

```bash
uv run --frozen --group dev ruff check .
uv run --frozen --group dev mypy src
uv run --frozen --group dev pytest -q
shellcheck install.sh scripts/*.sh \
  src/d_brain/resources/vault_template/.claude/hooks/*.sh
bash -n install.sh scripts/*.sh
uv build
git diff --check
```

Tests must create temporary vaults or read the packaged vault and project
templates. They must never depend on a local runtime `vault/`.

## Project layout

| Path | Purpose |
|---|---|
| `src/d_brain/` | Installable Python package |
| `src/d_brain/resources/vault_template/` | Public anonymized vault seed |
| `src/d_brain/resources/project_template/skills/` | Public project skills |
| `tests/` | Unit and integration tests |
| `deploy/` | systemd user-unit templates |
| `scripts/` | Source-checkout wrappers and setup tools |
| `docs/en`, `docs/ru` | Mirrored operator documentation |

## Add or change a CLI command

1. implement reusable logic in a dedicated module;
2. route it from `d_brain.cli`;
3. keep shell scripts as thin wrappers when compatibility is required;
4. document syntax, exit codes, and examples in both languages;
5. test invocation from a built wheel.

## Build and inspect distributions

```bash
uv build
unzip -l dist/a_second_brain-*.whl
tar -tzf dist/a_second_brain-*.tar.gz
```

Confirm that package resources, `LICENSE`, and `NOTICE` are present. Confirm
that `.env`, a populated root `vault/`, backups, logs, caches, and local paths
are absent.

## Clean-room test

Create a temporary environment, install the built wheel, initialize a new
instance, and run:

```bash
a-second-brain doctor /path/to/instance
```

Also verify that `.env` has mode `0600`, hooks remain executable, and a new Git
repository sees `.env` and `vault/` as ignored.

## Documentation synchronization

English and Russian directories must contain the same filenames. Every page
links to its counterpart. Update both versions in the same change and run the
documentation tests.

## Privacy review

Before publication:

- run a secret scanner over the complete candidate history;
- search for personal names, organizations, host paths, tokens, and emails;
- inspect tracked filenames;
- preserve upstream license attribution;
- use fictional people, organizations, and projects in tests.

Deleting a secret in a later commit does not revoke it. Rotate the credential
and rewrite unpublished history, or follow the host's incident process if it
was already published.
