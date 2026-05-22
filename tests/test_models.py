"""Tests for SQLAlchemy models."""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from stoa.models import ApiKey, AuditLog, Base, Comment, Post, Subscription


@pytest.fixture
def engine(tmp_path: Path):
    """Create an in-memory SQLAlchemy engine with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def session(engine):
    """Provide a session for model tests."""
    with Session(engine) as session:
        yield session


class TestPostModel:
    """Test Post model."""

    def test_create_post(self, session):
        """Create a valid Post instance."""
        post = Post(
            message_id="msg-model-001",
            author="test@example.com",
            subject="Model Test",
            tldr="Short summary",
            body_markdown="# Hello",
            body_html="<h1>Hello</h1>",
            token_cost=42,
            space="inbox",
        )
        session.add(post)
        session.commit()

        result = session.query(Post).filter_by(message_id="msg-model-001").first()
        assert result is not None
        assert result.author == "test@example.com"
        assert result.space == "inbox"
        assert result.token_cost == 42

    def test_post_default_space(self, session):
        """Default space should be 'inbox'."""
        post = Post(
            message_id="msg-default-space",
            author="test@example.com",
            subject="Default Space",
            tldr="Summary",
            body_markdown="Body",
            body_html="<p>Body</p>",
        )
        session.add(post)
        session.commit()

        result = session.query(Post).filter_by(message_id="msg-default-space").first()
        assert result.space == "inbox"

    def test_post_comments_relationship(self, session):
        """Post.comments should return related Comment instances."""
        post = Post(
            message_id="msg-rel-test",
            author="test@example.com",
            subject="Relationship Test",
            tldr="Summary",
            body_markdown="Body",
            body_html="<p>Body</p>",
        )
        session.add(post)
        session.commit()

        comment = Comment(
            post_id=post.id,
            author="commenter@example.com",
            body_markdown="Nice!",
            body_html="<p>Nice!</p>",
        )
        session.add(comment)
        session.commit()

        result = session.query(Post).filter_by(message_id="msg-rel-test").first()
        assert len(result.comments) == 1
        assert result.comments[0].author == "commenter@example.com"

    def test_post_repr(self, session):
        """Post repr should be readable."""
        post = Post(
            message_id="msg-repr",
            author="test@example.com",
            subject="Repr Test",
            tldr="Summary",
            body_markdown="Body",
            body_html="<p>Body</p>",
        )
        session.add(post)
        session.commit()
        assert "Post" in repr(post)
        assert "test@example.com" in repr(post)


class TestCommentModel:
    """Test Comment model."""

    def _create_post(self, session) -> Post:
        """Helper: create and return a Post."""
        post = Post(
            message_id="msg-for-comment",
            author="author@example.com",
            subject="Has Comments",
            tldr="Summary",
            body_markdown="Body",
            body_html="<p>Body</p>",
        )
        session.add(post)
        session.commit()
        return post

    def test_create_comment(self, session):
        """Create a valid Comment linked to a Post."""
        post = self._create_post(session)
        comment = Comment(
            post_id=post.id,
            author="commenter@example.com",
            body_markdown="Reply text",
            body_html="<p>Reply text</p>",
        )
        session.add(comment)
        session.commit()

        result = session.query(Comment).filter_by(post_id=post.id).first()
        assert result.author == "commenter@example.com"

    def test_comment_post_backref(self, session):
        """Comment.post should refer back to the parent Post."""
        post = self._create_post(session)
        comment = Comment(
            post_id=post.id,
            author="commenter@example.com",
            body_markdown="Reply",
            body_html="<p>Reply</p>",
        )
        session.add(comment)
        session.commit()

        result = session.query(Comment).first()
        assert result.post.id == post.id


class TestSubscriptionModel:
    """Test Subscription model."""

    def test_create_subscription(self, session):
        """Create a valid Subscription."""
        sub = Subscription(
            agent_email="agent@example.com",
            space="inbox",
            email_notifications=True,
        )
        session.add(sub)
        session.commit()

        result = session.query(Subscription).filter_by(agent_email="agent@example.com").first()
        assert result.space == "inbox"
        assert result.email_notifications is True

    def test_subscription_nullable_fields(self, session):
        """Space, author, keyword can be NULL."""
        sub = Subscription(agent_email="minimal@example.com")
        session.add(sub)
        session.commit()

        result = session.query(Subscription).filter_by(agent_email="minimal@example.com").first()
        assert result.space is None
        assert result.author is None
        assert result.keyword is None


class TestApiKeyModel:
    """Test ApiKey model."""

    def test_create_api_key(self, session):
        """Create a valid ApiKey with hashed storage."""
        key = ApiKey(
            agent_email="agent@example.com",
            api_key_prefix="herd_tes",
            api_key_hash="$2b$12$fakehashfortest",
        )
        session.add(key)
        session.commit()

        result = session.query(ApiKey).filter_by(agent_email="agent@example.com").first()
        assert result.api_key_prefix == "herd_tes"
        assert result.api_key_hash == "$2b$12$fakehashfortest"

    def test_api_key_created_at(self, session):
        """created_at should be auto-populated."""
        key = ApiKey(
            agent_email="dated@example.com",
            api_key_prefix="herd_dat",
            api_key_hash="$2b$12$fakehashfortest",
        )
        session.add(key)
        session.commit()

        result = session.query(ApiKey).filter_by(agent_email="dated@example.com").first()
        assert result.created_at is not None
        assert isinstance(result.created_at, datetime)


class TestAuditLogModel:
    """Test AuditLog model."""

    def test_create_audit_entry(self, session):
        """Create a valid AuditLog entry."""
        entry = AuditLog(
            event_type="injection_attempt",
            agent_email="bad@example.com",
            details='{"payload": "<script>alert(1)</script>"}',
        )
        session.add(entry)
        session.commit()

        result = session.query(AuditLog).filter_by(event_type="injection_attempt").first()
        assert result.agent_email == "bad@example.com"

    def test_audit_log_nullable_agent(self, session):
        """agent_email can be NULL for unauthenticated events."""
        entry = AuditLog(
            event_type="rate_limit",
            details='{"ip": "1.2.3.4"}',
        )
        session.add(entry)
        session.commit()

        result = session.query(AuditLog).filter_by(event_type="rate_limit").first()
        assert result.agent_email is None


class TestSchemaIntrospection:
    """Verify that SQLAlchemy models match the raw SQL schema."""

    def test_all_tables_exist(self, engine):
        """All 5 tables should be created by Base.metadata."""
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        expected = {"posts", "comments", "subscriptions", "api_keys", "audit_log"}
        assert expected.issubset(table_names)

    def test_posts_columns(self, engine):
        """Posts table should have all expected columns."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("posts")}
        expected = {
            "id",
            "message_id",
            "author",
            "subject",
            "tldr",
            "body_markdown",
            "body_html",
            "token_cost",
            "space",
            "timestamp",
            "in_reply_to",
        }
        assert expected.issubset(columns)

    def test_comments_columns(self, engine):
        """Comments table should have all expected columns."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("comments")}
        expected = {"id", "post_id", "author", "body_markdown", "body_html", "timestamp"}
        assert expected.issubset(columns)

    def test_subscriptions_columns(self, engine):
        """Subscriptions table should have all expected columns."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("subscriptions")}
        expected = {"id", "agent_email", "space", "author", "keyword", "email_notifications"}
        assert expected.issubset(columns)

    def test_api_keys_columns(self, engine):
        """api_keys table should have all expected columns."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("api_keys")}
        expected = {"id", "agent_email", "api_key", "created_at"}
        assert expected.issubset(columns)

    def test_audit_log_columns(self, engine):
        """audit_log table should have all expected columns."""
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("audit_log")}
        expected = {"id", "event_type", "agent_email", "details", "timestamp"}
        assert expected.issubset(columns)
