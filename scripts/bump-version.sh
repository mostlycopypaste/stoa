#!/usr/bin/env bash
# Bump version in pyproject.toml and create a git tag.
# Usage: ./scripts/bump-version.sh <major|minor|patch>

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <major|minor|patch>"
    exit 1
fi

BUMP_TYPE=$1

# Extract current version from pyproject.toml
CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
echo "Current version: $CURRENT_VERSION"

# Parse semver
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

# Bump version based on type
case $BUMP_TYPE in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
    *)
        echo "Invalid bump type: $BUMP_TYPE (must be major, minor, or patch)"
        exit 1
        ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
echo "New version: $NEW_VERSION"

# Update pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
rm pyproject.toml.bak

# Update main.py version string
sed -i.bak "s/version=\".*\"/version=\"$NEW_VERSION\"/" src/herd_inbox/main.py
sed -i.bak "s/Herd-Inbox API v.*/Herd-Inbox API v$NEW_VERSION\"/" src/herd_inbox/main.py
rm src/herd_inbox/main.py.bak

echo "✓ Updated version to $NEW_VERSION in pyproject.toml and main.py"
echo "  Next steps:"
echo "  1. git add pyproject.toml src/herd_inbox/main.py"
echo "  2. git commit -m \"chore: bump version to $NEW_VERSION\""
echo "  3. git tag v$NEW_VERSION"
echo "  4. git push && git push --tags"
