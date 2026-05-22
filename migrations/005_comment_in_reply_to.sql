-- Add in_reply_to column to comments table for threaded replies.
-- NULL means top-level comment (direct reply to the post).
-- Non-NULL references another comment's id, creating a reply chain.

ALTER TABLE comments ADD COLUMN in_reply_to INTEGER REFERENCES comments(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_comments_in_reply_to ON comments(in_reply_to);