-- Migration 006: Footer messages table for rotating email footers

CREATE TABLE IF NOT EXISTS footer_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL CHECK(length(text) <= 500),
  category TEXT NOT NULL CHECK(category IN ('token_economics', 'social_proof', 'fomo', 'cheeky')),
  context TEXT CHECK(context IS NULL OR context IN ('announcement', 'discussion')),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  last_used_at DATETIME,
  created_at DATETIME NOT NULL DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX idx_footer_messages_active ON footer_messages(active);
CREATE INDEX idx_footer_messages_category ON footer_messages(category);
CREATE INDEX idx_footer_messages_last_used ON footer_messages(last_used_at);
