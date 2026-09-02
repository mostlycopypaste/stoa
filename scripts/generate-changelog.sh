#!/usr/bin/env bash
# Generate markdown release notes from conventional-commit SUBJECT lines.
# Usage: ./scripts/generate-changelog.sh [from-tag] [to-ref] [output-file]

set -euo pipefail

FROM_TAG=${1:-$(git describe --tags --abbrev=0 2>/dev/null || echo "")}
TO_REF=${2:-HEAD}
OUT_FILE=${3:-CHANGELOG.md}
DATE_STR=$(date +%Y-%m-%d)

if [ -z "$FROM_TAG" ]; then
  echo "No previous tag found — generating changelog from initial commit"
  FROM_TAG=$(git rev-list --max-parents=0 "$TO_REF")
fi

echo "Generating changelog from $FROM_TAG to $TO_REF -> $OUT_FILE..."

COMMITS=$(git log --format='%h %s' "$FROM_TAG..$TO_REF")

section() {
  local title="$1"
  local pattern="$2"
  local lines
  lines=$(printf '%s\n' "$COMMITS" | grep -E "$pattern" || true)
  if [ -n "$lines" ]; then
    echo "## $title"
    echo
    printf '%s\n' "$lines" | sed 's/^/- /'
    echo
  fi
}

{
  echo "# Changelog"
  echo
  echo "Generated on $DATE_STR"
  echo

  # Match against subject prefixes only; avoid body-based false matches.
  section "Features" '^[0-9a-f]+ feat(\(|:).*'
  section "Bug Fixes" '^[0-9a-f]+ fix(\(|:).*'
  section "Documentation" '^[0-9a-f]+ docs(\(|:).*'
  section "Refactoring" '^[0-9a-f]+ refactor(\(|:).*'
  section "Tests" '^[0-9a-f]+ test(\(|:).*'
  section "CI/CD" '^[0-9a-f]+ ci(\(|:).*'
  section "Chores" '^[0-9a-f]+ chore(\(|:).*'
} > "$OUT_FILE"

echo "✓ Generated $OUT_FILE"
