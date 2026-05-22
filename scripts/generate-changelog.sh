#!/usr/bin/env bash
# Generate CHANGELOG.md from conventional commit messages.
# Usage: ./scripts/generate-changelog.sh [from-tag] [to-tag]

set -euo pipefail

FROM_TAG=${1:-$(git describe --tags --abbrev=0 2>/dev/null || echo "")}
TO_TAG=${2:-HEAD}

if [ -z "$FROM_TAG" ]; then
    echo "No previous tag found — generating full changelog from first commit"
    FROM_TAG=$(git rev-list --max-parents=0 HEAD)
fi

echo "Generating changelog from $FROM_TAG to $TO_TAG..."

{
    echo "# Changelog"
    echo ""
    echo "Generated on $(date +%Y-%m-%d)"
    echo ""

    # Group by type
    for TYPE in feat fix docs chore refactor test ci; do
        COMMITS=$(git log --oneline "$FROM_TAG..$TO_TAG" --grep="^$TYPE" --perl-regexp 2>/dev/null || echo "")
        if [ -n "$COMMITS" ]; then
            case $TYPE in
                feat) HEADER="## Features" ;;
                fix) HEADER="## Bug Fixes" ;;
                docs) HEADER="## Documentation" ;;
                chore) HEADER="## Chores" ;;
                refactor) HEADER="## Refactoring" ;;
                test) HEADER="## Tests" ;;
                ci) HEADER="## CI/CD" ;;
            esac
            echo "$HEADER"
            echo ""
            echo "$COMMITS" | sed 's/^/- /'
            echo ""
        fi
    done
} > CHANGELOG.md

echo "✓ Generated CHANGELOG.md"
