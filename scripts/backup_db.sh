#!/usr/bin/env bash
# Daily backup of herd-inbox SQLite database from Fly.io
# Usage: ./scripts/backup_db.sh
# Designed to be run via cron (OpenClaw or system)
# Retains 7 days of backups, pruning older ones

set -euo pipefail

APP="herd-inbox"
BACKUP_DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
RETENTION_DAYS=7

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

# Timestamp for this backup
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/herd_inbox_${TIMESTAMP}.db"

echo "[$(date -Iseconds)] Starting herd-inbox database backup..."

# Download the database directly via fly ssh console + cat
# SQLite in WAL mode: the -wal and -shm files contain recent writes,
# so we download all three for a consistent backup.
echo "[$(date -Iseconds)] Downloading database files from Fly.io..."
fly ssh console -a "$APP" --command "cat /data/herd_inbox.db" > "$BACKUP_FILE"

# Verify the backup is a valid SQLite database
echo "[$(date -Iseconds)] Verifying backup integrity..."
VALIDATION=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1)
if [ "$VALIDATION" != "ok" ]; then
    echo "ERROR: Backup integrity check failed: $VALIDATION"
    rm -f "$BACKUP_FILE"
    exit 1
fi

TABLE_COUNT=$(sqlite3 "$BACKUP_FILE" "SELECT count(*) FROM sqlite_master WHERE type='table';")
echo "[$(date -Iseconds)] Backup verified: $TABLE_COUNT tables, $(du -h "$BACKUP_FILE" | cut -f1)"

# Prune backups older than RETENTION_DAYS
echo "[$(date -Iseconds)] Pruning backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "herd_inbox_*.db" -mtime +$RETENTION_DAYS -delete -print 2>/dev/null || true

# Show remaining backups
echo "[$(date -Iseconds)] Current backups:"
ls -lh "$BACKUP_DIR"/herd_inbox_*.db 2>/dev/null | awk '{print "  " $9 " (" $5 ")"}' || echo "  (none)"

echo "[$(date -Iseconds)] Backup complete: $BACKUP_FILE"