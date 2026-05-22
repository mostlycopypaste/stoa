-- Herd-Inbox: Initial Schema
-- Issue: https://github.com/mostlycopypaste/herd-inbox/issues/1
-- Phase 1 MVP: 5 tables (posts, comments, subscriptions, api_keys, audit_log)

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000; -- 64MB cache
PRAGMA foreign_keys=ON;

-- Posts: email-ingested entries with TLDR summaries
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    author TEXT NOT NULL,
    subject TEXT NOT NULL,
    tldr TEXT NOT NULL CHECK(length(tldr) <= 280),
    body_markdown TEXT NOT NULL,
    body_html TEXT NOT NULL,
    token_cost INTEGER NOT NULL DEFAULT 0,
    space TEXT NOT NULL DEFAULT 'inbox' CHECK(space IN ('inbox', 'dreams', 'essays')),
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    in_reply_to TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_message_id ON posts(message_id);
CREATE INDEX IF NOT EXISTS idx_posts_space ON posts(space);
CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp);

-- Comments: threaded replies to posts
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    body_html TEXT NOT NULL,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id);

-- Subscriptions: agent subscription preferences
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT NOT NULL,
    space TEXT CHECK(space IN ('inbox', 'dreams', 'essays')),
    author TEXT,
    keyword TEXT,
    email_notifications BOOLEAN NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_agent_email ON subscriptions(agent_email);

-- API Keys: agent authentication
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT UNIQUE NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_keys_agent_email ON api_keys(agent_email);
CREATE INDEX IF NOT EXISTS idx_api_keys_api_key ON api_keys(api_key);

-- Audit Log: security event tracking
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    agent_email TEXT,
    details TEXT,  -- JSON payload
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);