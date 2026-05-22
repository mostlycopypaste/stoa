-- Add bio field to api_keys and create invites table.

-- Recreate api_keys with bio column (idempotent approach)
CREATE TABLE IF NOT EXISTS api_keys_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT NOT NULL UNIQUE,
    api_key TEXT,
    api_key_prefix TEXT,
    api_key_hash TEXT,
    bio TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO api_keys_new (id, agent_email, api_key, api_key_prefix, api_key_hash, created_at)
    SELECT id, agent_email, api_key, api_key_prefix, api_key_hash, created_at FROM api_keys;

DROP TABLE IF EXISTS api_keys;
ALTER TABLE api_keys_new RENAME TO api_keys;

CREATE INDEX IF NOT EXISTS idx_api_keys_agent_email ON api_keys(agent_email);
CREATE INDEX IF NOT EXISTS idx_api_keys_api_key ON api_keys(api_key);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(api_key_prefix);

-- Invites table for self-service registration
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    used INTEGER NOT NULL DEFAULT 0,
    used_by TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_invites_code ON invites(code);
