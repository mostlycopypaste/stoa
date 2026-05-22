-- Migration 007: Add weekly_digest opt-in field to api_keys
-- Note: ALTER TABLE ... DEFAULT on large tables can lock in Postgres.
-- Fine for SQLite. If migrating to Postgres, consider a multi-step approach:
-- 1. ADD COLUMN ... DEFAULT NULL
-- 2. UPDATE in batches
-- 3. ALTER COLUMN SET NOT NULL

ALTER TABLE api_keys ADD COLUMN weekly_digest BOOLEAN NOT NULL DEFAULT TRUE;
