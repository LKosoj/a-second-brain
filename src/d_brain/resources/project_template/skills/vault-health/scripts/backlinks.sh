#!/bin/bash
# Find all files in the vault that link to a given target.
# Usage: bash backlinks.sh "business/crm"
#        bash backlinks.sh "thoughts/ideas/sample-idea"

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
VAULT_DIR="$PROJECT_DIR/vault"
TARGET="${1%.md}"

if [ -z "$TARGET" ]; then
    echo "Usage: backlinks.sh <note-path>"
    echo "Example: backlinks.sh business/crm"
    exit 1
fi

# Search for wikilinks to target (with or without .md, with or without alias)
grep -rlF --include='*.md' "[[$TARGET" "$VAULT_DIR" 2>/dev/null | \
    sed "s|$VAULT_DIR/||" | \
    sort
