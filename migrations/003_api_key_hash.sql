-- Add hashed key storage columns to api_keys table.
-- Recreate table approach (SQLite lacks ALTER TABLE ADD COLUMN IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS api_keys_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT NOT NULL UNIQUE,
    api_key TEXT,
    api_key_prefix TEXT,
    api_key_hash TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO api_keys_new (id, agent_email, api_key, created_at)
    SELECT id, agent_email, api_key, created_at FROM api_keys;

DROP TABLE IF EXISTS api_keys;
ALTER TABLE api_keys_new RENAME TO api_keys;

CREATE INDEX IF NOT EXISTS idx_api_keys_agent_email ON api_keys(agent_email);
CREATE INDEX IF NOT EXISTS idx_api_keys_api_key ON api_keys(api_key);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(api_key_prefix);
