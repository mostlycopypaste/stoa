"""Tests for database initialization and schema."""

from pathlib import Path

import pytest

from stoa.db import (
    drop_tables,
    get_connection,
    init_db,
    run_migrations,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Provide a temporary database path."""
    return tmp_path / "test.db"


@pytest.fixture
def db_conn(tmp_db: Path):
    """Provide an initialized database connection, dropping tables after."""
    conn = init_db(tmp_db)
    yield conn
    conn.close()
    drop_tables(tmp_db)


class TestMigrations:
    """Test that migrations create the expected schema."""

    def test_migration_creates_all_tables(self, db_conn):
        """All 5 tables should exist after migration."""
        cursor = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row["name"] for row in cursor.fetchall()}
        expected = {"api_keys", "audit_log", "comments", "posts", "subscriptions"}
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_migration_is_idempotent(self, tmp_db: Path):
        """Running migrations twice should not fail."""
        run_migrations(tmp_db)
        run_migrations(tmp_db)  # Should not raise
        conn = get_connection(tmp_db)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row["name"] for row in cursor.fetchall()}
        assert "posts" in tables
        conn.close()

    def test_wal_mode_enabled(self, db_conn):
        """WAL mode should be enabled."""
        cursor = db_conn.execute("PRAGMA journal_mode")
        result = cursor.fetchone()
        assert result[0] == "wal"

    def test_foreign_keys_enabled(self, db_conn):
        """Foreign keys should be enforced."""
        cursor = db_conn.execute("PRAGMA foreign_keys")
        result = cursor.fetchone()
        assert result[0] == 1


class TestPostsTable:
    """Test posts table schema and constraints."""

    def test_insert_valid_post(self, db_conn):
        """Insert a valid post row."""
        db_conn.execute(
            "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost, space) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "msg-001",
                "test@example.com",
                "Test Subject",
                "A short summary",
                "# Body",
                "<h1>Body</h1>",
                100,
                "inbox",
            ),
        )
        db_conn.commit()
        cursor = db_conn.execute("SELECT * FROM posts WHERE message_id = ?", ("msg-001",))
        row = cursor.fetchone()
        assert row["author"] == "test@example.com"
        assert row["space"] == "inbox"

    def test_insert_post_default_space(self, db_conn):
        """Default space should be 'inbox'."""
        db_conn.execute(
            "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("msg-002", "test@example.com", "No Space", "Summary", "Body", "<p>Body</p>", 50),
        )
        db_conn.commit()
        cursor = db_conn.execute("SELECT space FROM posts WHERE message_id = ?", ("msg-002",))
        assert cursor.fetchone()["space"] == "inbox"

    def test_insert_post_invalid_space_fails(self, db_conn):
        """Invalid space value should fail constraint check."""
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost, space) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "msg-003",
                    "test@example.com",
                    "Bad Space",
                    "Summary",
                    "Body",
                    "<p>Body</p>",
                    50,
                    "invalid",
                ),
            )
            db_conn.commit()

    def test_insert_post_tldr_too_long_fails(self, db_conn):
        """TLDR over 280 chars should fail constraint check."""
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("msg-004", "test@example.com", "Long TLDR", "x" * 281, "Body", "<p>Body</p>", 50),
            )
            db_conn.commit()

    def test_insert_duplicate_message_id_fails(self, db_conn):
        """Duplicate message_id should fail unique constraint."""
        db_conn.execute(
            "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("msg-dup", "test@example.com", "First", "Summary", "Body", "<p>Body</p>", 50),
        )
        db_conn.commit()
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("msg-dup", "test@example.com", "Second", "Summary", "Body", "<p>Body</p>", 50),
            )
            db_conn.commit()


class TestCommentsTable:
    """Test comments table and foreign key constraints."""

    def _insert_post(self, db_conn) -> int:
        """Helper: insert a post and return its id."""
        cursor = db_conn.execute(
            "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "msg-cmt",
                "test@example.com",
                "Post for Comments",
                "Summary",
                "Body",
                "<p>Body</p>",
                50,
            ),
        )
        db_conn.commit()
        return cursor.lastrowid

    def test_insert_comment(self, db_conn):
        """Insert a valid comment linked to a post."""
        post_id = self._insert_post(db_conn)
        db_conn.execute(
            "INSERT INTO comments (post_id, author, body_markdown, body_html) VALUES (?, ?, ?, ?)",
            (post_id, "commenter@example.com", "Great post!", "<p>Great post!</p>"),
        )
        db_conn.commit()
        cursor = db_conn.execute("SELECT * FROM comments WHERE post_id = ?", (post_id,))
        row = cursor.fetchone()
        assert row["author"] == "commenter@example.com"

    def test_comment_cascade_delete(self, db_conn):
        """Deleting a post should cascade delete its comments."""
        post_id = self._insert_post(db_conn)
        db_conn.execute(
            "INSERT INTO comments (post_id, author, body_markdown, body_html) VALUES (?, ?, ?, ?)",
            (post_id, "commenter@example.com", "Will be deleted", "<p>Will be deleted</p>"),
        )
        db_conn.commit()

        # Delete the post
        db_conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        db_conn.commit()

        # Comment should be gone
        cursor = db_conn.execute("SELECT * FROM comments WHERE post_id = ?", (post_id,))
        assert cursor.fetchone() is None

    def test_comment_invalid_post_id_fails(self, db_conn):
        """Comment with non-existent post_id should fail foreign key constraint."""
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO comments (post_id, author, body_markdown, body_html) "
                "VALUES (?, ?, ?, ?)",
                (99999, "commenter@example.com", "Orphan comment", "<p>Orphan</p>"),
            )
            db_conn.commit()


class TestSubscriptionsTable:
    """Test subscriptions table schema."""

    def test_insert_subscription(self, db_conn):
        """Insert a valid subscription."""
        db_conn.execute(
            "INSERT INTO subscriptions (agent_email, space, email_notifications) VALUES (?, ?, ?)",
            ("agent@example.com", "inbox", 1),
        )
        db_conn.commit()
        cursor = db_conn.execute(
            "SELECT * FROM subscriptions WHERE agent_email = ?", ("agent@example.com",)
        )
        row = cursor.fetchone()
        assert row["space"] == "inbox"

    def test_subscription_default_notifications(self, db_conn):
        """Default email_notifications should be True."""
        db_conn.execute(
            "INSERT INTO subscriptions (agent_email) VALUES (?)",
            ("agent2@example.com",),
        )
        db_conn.commit()
        cursor = db_conn.execute(
            "SELECT email_notifications FROM subscriptions WHERE agent_email = ?",
            ("agent2@example.com",),
        )
        assert cursor.fetchone()["email_notifications"] == 1


class TestApiKeysTable:
    """Test api_keys table schema."""

    def test_insert_api_key(self, db_conn):
        """Insert a valid API key."""
        db_conn.execute(
            "INSERT INTO api_keys (agent_email, api_key) VALUES (?, ?)",
            ("agent@example.com", "herd_abc123"),
        )
        db_conn.commit()
        cursor = db_conn.execute(
            "SELECT * FROM api_keys WHERE agent_email = ?", ("agent@example.com",)
        )
        row = cursor.fetchone()
        assert row["api_key"] == "herd_abc123"

    def test_duplicate_agent_email_fails(self, db_conn):
        """Duplicate agent_email should fail unique constraint."""
        db_conn.execute(
            "INSERT INTO api_keys (agent_email, api_key) VALUES (?, ?)",
            ("unique@example.com", "herd_key1"),
        )
        db_conn.commit()
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO api_keys (agent_email, api_key) VALUES (?, ?)",
                ("unique@example.com", "herd_key2"),
            )
            db_conn.commit()


class TestAuditLogTable:
    """Test audit_log table schema."""

    def test_insert_audit_entry(self, db_conn):
        """Insert a valid audit log entry."""
        db_conn.execute(
            "INSERT INTO audit_log (event_type, agent_email, details) VALUES (?, ?, ?)",
            ("injection_attempt", "bad@example.com", '{"payload": "<script>"}'),
        )
        db_conn.commit()
        cursor = db_conn.execute(
            "SELECT * FROM audit_log WHERE event_type = ?", ("injection_attempt",)
        )
        row = cursor.fetchone()
        assert row["agent_email"] == "bad@example.com"

    def test_audit_log_nullable_agent(self, db_conn):
        """Audit log entries can have NULL agent_email (e.g., unauthenticated)."""
        db_conn.execute(
            "INSERT INTO audit_log (event_type, details) VALUES (?, ?)",
            ("rate_limit", '{"ip": "1.2.3.4"}'),
        )
        db_conn.commit()
        cursor = db_conn.execute("SELECT * FROM audit_log WHERE event_type = ?", ("rate_limit",))
        row = cursor.fetchone()
        assert row["agent_email"] is None


class TestDropTables:
    """Test the drop_tables utility."""

    def test_drop_tables_removes_all(self, tmp_db: Path):
        """drop_tables should remove all stoa tables."""
        init_db(tmp_db)
        drop_tables(tmp_db)

        conn = get_connection(tmp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('posts', 'comments', 'subscriptions', 'api_keys', 'audit_log')"
        )
        tables = cursor.fetchall()
        assert len(tables) == 0
        conn.close()
