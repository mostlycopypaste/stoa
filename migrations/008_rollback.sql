-- Rollback migration 008: Remove updated_at column from posts table
-- SQLite doesn't support DROP COLUMN before 3.35.0, so recreate the table

-- Create posts without updated_at
CREATE TABLE posts_backup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    author TEXT NOT NULL,
    subject TEXT NOT NULL,
    tldr TEXT NOT NULL CHECK(length(tldr) <= 280),
    body_markdown TEXT NOT NULL,
    body_html TEXT NOT NULL,
    token_cost INTEGER NOT NULL DEFAULT 0,
    space TEXT NOT NULL DEFAULT 'inbox' CHECK(space IN ('inbox', 'dreams', 'essays')),
    timestamp DATETIME NOT NULL,
    in_reply_to TEXT
);

-- Copy data (excluding updated_at)
INSERT INTO posts_backup SELECT id, message_id, author, subject, tldr, body_markdown, body_html, token_cost, space, timestamp, in_reply_to FROM posts;

-- Drop old table and rename
DROP TABLE posts;
ALTER TABLE posts_backup RENAME TO posts;

-- Recreate indexes
CREATE INDEX idx_posts_message_id ON posts(message_id);
CREATE INDEX idx_posts_space ON posts(space);
CREATE INDEX idx_posts_timestamp ON posts(timestamp);