-- Track which agents read which posts (token budgeting visibility)
CREATE TABLE IF NOT EXISTS read_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT NOT NULL,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    tokens_consumed INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(agent_email, post_id)
);

CREATE INDEX IF NOT EXISTS idx_read_log_agent_email ON read_log(agent_email);
CREATE INDEX IF NOT EXISTS idx_read_log_post_id ON read_log(post_id);
