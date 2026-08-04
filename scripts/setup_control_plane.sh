#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

required_paths=(
  "vault-manifest.json"
  "src/d_brain/control_plane/contracts.py"
  "src/d_brain/control_plane/registry.py"
  "src/d_brain/control_plane/router.py"
  "vault/.claude/settings.json"
  "vault/.claude/hooks/protect-control-plane.sh"
  "vault/.claude/hooks/validate-frontmatter.sh"
  "vault/.claude/hooks/context-pack.sh"
  "vault/.claude/docs/prompt-source-map.md"
  "docs/control-plane.md"
  "docs/control-plane-security-profile-template.md"
)

missing=0
for path in "${required_paths[@]}"; do
  if [[ ! -e "$path" ]]; then
    echo "Missing control-plane asset: $path" >&2
    missing=1
  fi
done

if [[ "$missing" -ne 0 ]]; then
  exit 1
fi

for command in jq uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

if ! uv run --frozen --no-dev python -c 'from pathlib import Path; from d_brain.manifest import load_manifest; load_manifest(Path("."))' 2>/dev/null; then
  echo "Invalid vault manifest: vault-manifest.json" >&2
  exit 1
fi

chmod +x \
  vault/.claude/hooks/protect-control-plane.sh \
  vault/.claude/hooks/validate-frontmatter.sh \
  vault/.claude/hooks/context-pack.sh

cat <<'EOF'
Control-plane assets are present.

Managed setup:
- canonical registry/contracts/router
- repo-managed hook settings
- ownership/security docs

Next steps:
- review docs/control-plane.md
- use ./scripts/update_control_plane.sh after pulling changes
EOF
