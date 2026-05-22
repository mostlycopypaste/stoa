-- Migration 009: Add status column to posts table

ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed'));
CREATE INDEX idx_posts_status ON posts(status);
