"""Tests for Mirror Test archive import."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from stoa.db import drop_tables, init_db


def make_eml(
    message_id: str = "<test-001@example.com>",
    from_addr: str = "Alice Smith <alice@example.com>",
    subject: str = "Re: Mirror Test discussion",
    body: str = "This is a test email body.\nWith multiple lines.",
    date: str = "Mon, 15 Jan 2024 10:30:00 +0000",
    in_reply_to: str | None = None,
) -> str:
    """Create a minimal .eml file content."""
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = date
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    return msg.as_string()


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def db_conn(tmp_db: Path):
    conn = init_db(tmp_db)
    yield conn
    conn.close()
    drop_tables(tmp_db)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


class TestImportSingleEml:
    """Test importing a single .eml file."""

    def test_import_single_eml(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml_content = make_eml()
        (data_dir / "test.eml").write_text(eml_content)

        result = import_emails(db_path=tmp_db, data_dir=data_dir)

        assert result["imported"] == 1
        assert result["skipped"] == 0

        conn = init_db(tmp_db)
        cursor = conn.execute(
            "SELECT * FROM posts WHERE message_id = ?", ("<test-001@example.com>",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["author"] == "Alice Smith"
        assert row["subject"] == "Re: Mirror Test discussion"
        conn.close()

    def test_import_empty_directory(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        result = import_emails(db_path=tmp_db, data_dir=data_dir)
        assert result["imported"] == 0
        assert result["skipped"] == 0


class TestImportDuplicateHandling:
    """Test that duplicates are handled gracefully."""

    def test_import_duplicate_skipped(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml_content = make_eml(message_id="<dup-001@example.com>")
        (data_dir / "msg1.eml").write_text(eml_content)

        import_emails(db_path=tmp_db, data_dir=data_dir)
        result = import_emails(db_path=tmp_db, data_dir=data_dir)

        assert result["imported"] == 0
        assert result["skipped"] == 1

    def test_import_duplicate_no_duplicate_rows(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml_content = make_eml(message_id="<dup-002@example.com>")
        (data_dir / "msg1.eml").write_text(eml_content)

        import_emails(db_path=tmp_db, data_dir=data_dir)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM posts WHERE message_id = ?",
            ("<dup-002@example.com>",),
        )
        assert cursor.fetchone()["cnt"] == 1
        conn.close()


class TestImportExtractsFields:
    """Test that all fields are correctly extracted from .eml."""

    def test_extracts_message_id(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(message_id="<unique-msg-id@host.com>")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute(
            "SELECT * FROM posts WHERE message_id = ?", ("<unique-msg-id@host.com>",)
        ).fetchone()
        assert row["message_id"] == "<unique-msg-id@host.com>"
        conn.close()

    def test_extracts_author_name_only(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(from_addr="Bob Jones <bob@example.com>")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT author FROM posts").fetchone()
        assert row["author"] == "Bob Jones"
        conn.close()

    def test_extracts_author_email_fallback(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(from_addr="lonely@example.com")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT author FROM posts").fetchone()
        assert row["author"] == "lonely@example.com"
        conn.close()

    def test_extracts_in_reply_to(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(
            message_id="<reply@example.com>",
            in_reply_to="<original@example.com>",
        )
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute(
            "SELECT in_reply_to FROM posts WHERE message_id = ?", ("<reply@example.com>",)
        ).fetchone()
        assert row["in_reply_to"] == "<original@example.com>"
        conn.close()

    def test_extracts_timestamp(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(date="Wed, 20 Mar 2024 14:00:00 +0000")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT timestamp FROM posts").fetchone()
        assert "2024-03-20" in row["timestamp"]
        conn.close()

    def test_body_stored_as_markdown_and_html(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(body="Hello world\n\nParagraph two.")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT body_markdown, body_html FROM posts").fetchone()
        assert "Hello world" in row["body_markdown"]
        assert "<p>" in row["body_html"]
        conn.close()


class TestTldrGeneration:
    """Test TLDR generation from email body."""

    def test_tldr_max_280_chars(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        long_body = "A" * 500
        eml = make_eml(body=long_body)
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT tldr FROM posts").fetchone()
        assert len(row["tldr"]) <= 280
        conn.close()

    def test_tldr_strips_quoted_text(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        body = "My actual reply.\n\n> This is quoted text from previous email.\n> More quoted.\n\nAnother line."
        eml = make_eml(body=body)
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT tldr FROM posts").fetchone()
        assert "> This is quoted" not in row["tldr"]
        assert "My actual reply." in row["tldr"]
        conn.close()

    def test_tldr_strips_whitespace(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        body = "   \n\n  Hello there.  \n\n  "
        eml = make_eml(body=body)
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT tldr FROM posts").fetchone()
        assert row["tldr"] == "Hello there."
        conn.close()


class TestSpaceClassification:
    """Test classification of posts into spaces."""

    def test_dream_journal_classified_as_dreams(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(subject="Dream Journal: Flying over mountains")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT space FROM posts").fetchone()
        assert row["space"] == "dreams"
        conn.close()

    def test_regular_subject_classified_as_inbox(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(subject="Re: Weekly standup notes")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT space FROM posts").fetchone()
        assert row["space"] == "inbox"
        conn.close()

    def test_dream_journal_case_insensitive(self, tmp_db: Path, data_dir: Path):
        from scripts.import_mirror_test import import_emails

        eml = make_eml(subject="DREAM JOURNAL: Underwater adventure")
        (data_dir / "msg.eml").write_text(eml)
        import_emails(db_path=tmp_db, data_dir=data_dir)

        conn = init_db(tmp_db)
        row = conn.execute("SELECT space FROM posts").fetchone()
        assert row["space"] == "dreams"
        conn.close()
