#!/usr/bin/env bash
# Weekly herd-inbox digest sender
# Fetches digest preview from the API and emails it to opted-in recipients
# Usage: ./scripts/send_digest.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source agent environment
AGENT_ENV="$HOME/.openclaw-primary/agents/oc/agent/.env"
if [[ -f "$AGENT_ENV" ]]; then
    set -a
    source "$AGENT_ENV"
    set +a
else
    echo "ERROR: Agent .env not found: $AGENT_ENV" >&2
    exit 1
fi

ADMIN_KEY="${HERD_INBOX_ADMIN_KEY:?HERD_INBOX_ADMIN_KEY not set}"
BASE_URL="${HERD_INBOX_URL:-https://herd.mostlycopyandpaste.com}"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# Fetch digest preview
echo "Fetching digest preview..."
DIGEST_JSON=$(curl -sf -H "X-Admin-Key: $ADMIN_KEY" "$BASE_URL/api/admin/digest/preview")

if [[ -z "$DIGEST_JSON" ]]; then
    echo "ERROR: Empty response from digest API" >&2
    exit 1
fi

# Extract fields
SUBJECT=$(echo "$DIGEST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('subject','Herd Weekly Digest'))")
BODY_TEXT=$(echo "$DIGEST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('body_text',''))")
BODY_HTML=$(echo "$DIGEST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('body_html',''))")
RECIPIENTS=$(echo "$DIGEST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('recipients',[])))")
OPTED_OUT=$(echo "$DIGEST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('opted_out',[])))")

echo "Subject: $SUBJECT"
echo "Recipients: $RECIPIENTS"
if [[ -n "$OPTED_OUT" ]]; then
    echo "Opted out: $OPTED_OUT"
fi

if $DRY_RUN; then
    echo "--- DRY RUN ---"
    echo "Would send to: $RECIPIENTS"
    echo ""
    echo "$BODY_TEXT"
    exit 0
fi

# Write body to temp file for --rich sending
BODY_FILE=$(mktemp /tmp/herd-digest-XXXXXX.md)
echo "$BODY_TEXT" > "$BODY_FILE"

# Send email to first recipient, CC the rest
# (herd_mail --to accepts single recipient; --cc for the rest)
FIRST_RECIPIENT=$(echo "$RECIPIENTS" | cut -d',' -f1)
CC_RECIPIENTS=""

if [[ "$RECIPIENTS" == *","* ]]; then
    # Remove first recipient from CC list
    CC_RECIPIENTS=$(echo "$RECIPIENTS" | cut -d',' -f2-)
fi

echo "Sending digest to $FIRST_RECIPIENT (CC: $CC_RECIPIENTS)..."

cd "$WORKSPACE_DIR"
python3 scripts/herd_mail.py send \
    --to "$FIRST_RECIPIENT" \
    ${CC_RECIPIENTS:+--cc "$CC_RECIPIENTS"} \
    --subject "$SUBJECT" \
    --body-file "$BODY_FILE" \
    --rich \
    --skip-duplicate-check

# Clean up
rm -f "$BODY_FILE"

echo "Digest sent successfully!"