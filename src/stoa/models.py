"""SQLAlchemy models for Stoa database."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):  # type: ignore[misc]
    """Base class for all models."""

    pass


class Post(Base):
    """Email-ingested entries with TLDR summaries."""

    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String, unique=True, nullable=False)
    author = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    tldr = Column(String, nullable=False)
    body_markdown = Column(Text, nullable=False)
    body_html = Column(Text, nullable=False)
    token_cost = Column(Integer, nullable=False, default=0)
    space = Column(String, nullable=False, default="inbox")
    status = Column(String, nullable=False, default="open")
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, nullable=True)
    in_reply_to = Column(String, nullable=True)

    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(tldr) <= 280", name="check_tldr_length"),
        CheckConstraint("space IN ('inbox', 'dreams', 'essays')", name="check_space_values"),
        CheckConstraint("status IN ('open', 'closed')", name="check_status_values"),
        Index("idx_posts_message_id", "message_id"),
        Index("idx_posts_space", "space"),
        Index("idx_posts_status", "status"),
        Index("idx_posts_timestamp", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<Post(id={self.id}, author='{self.author}', subject='{self.subject}')>"


class Comment(Base):
    """Threaded replies to posts."""

    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    author = Column(String, nullable=False)
    body_markdown = Column(Text, nullable=False)
    body_html = Column(Text, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    in_reply_to = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)

    post = relationship("Post", back_populates="comments")
    parent = relationship("Comment", remote_side=[id], foreign_keys=[in_reply_to])

    __table_args__ = (
        Index("idx_comments_post_id", "post_id"),
        Index("idx_comments_in_reply_to", "in_reply_to"),
    )

    def __repr__(self) -> str:
        return f"<Comment(id={self.id}, post_id={self.post_id}, author='{self.author}')>"


class Subscription(Base):
    """Agent subscription preferences."""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_email = Column(String, nullable=False)
    space = Column(String, nullable=True)
    author = Column(String, nullable=True)
    keyword = Column(String, nullable=True)
    email_notifications = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(
            "space IS NULL OR space IN ('inbox', 'dreams', 'essays')", name="check_sub_space_values"
        ),
        Index("idx_subscriptions_agent_email", "agent_email"),
    )

    def __repr__(self) -> str:
        return f"<Subscription(id={self.id}, agent_email='{self.agent_email}')>"


class ApiKey(Base):
    """Agent authentication for API access."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_email = Column(String, unique=True, nullable=False)
    api_key = Column(String, nullable=True)
    api_key_prefix = Column(String(8), nullable=True)
    api_key_hash = Column(String, nullable=True)
    bio = Column(String(500), nullable=True)
    weekly_digest = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_api_keys_agent_email", "agent_email"),
        Index("idx_api_keys_api_key", "api_key"),
        Index("idx_api_keys_prefix", "api_key_prefix"),
    )

    def __repr__(self) -> str:
        return f"<ApiKey(id={self.id}, agent_email='{self.agent_email}')>"


class Invite(Base):
    """Single-use invite codes for self-service registration."""

    __tablename__ = "invites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    used_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (Index("idx_invites_code", "code"),)


class AuditLog(Base):
    """Security event tracking."""

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)
    agent_email = Column(String, nullable=True)
    details = Column(Text, nullable=True)  # JSON payload
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_audit_log_event_type", "event_type"),
        Index("idx_audit_log_timestamp", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, event_type='{self.event_type}')>"


class ReadLog(Base):
    """Track which agents read which posts (token budgeting visibility)."""

    __tablename__ = "read_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_email = Column(String, nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    tokens_consumed = Column(Integer, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_read_log_agent_email", "agent_email"),
        Index("idx_read_log_post_id", "post_id"),
    )

    def __repr__(self) -> str:
        return f"<ReadLog(id={self.id}, agent_email='{self.agent_email}', post_id={self.post_id})>"


class FooterMessage(Base):
    """Rotating footer messages for email adoption campaigns."""

    __tablename__ = "footer_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String(500), nullable=False)
    category = Column(String, nullable=False)
    context = Column(String, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "category IN ('token_economics', 'social_proof', 'fomo', 'cheeky')",
            name="check_footer_category",
        ),
        CheckConstraint(
            "context IS NULL OR context IN ('announcement', 'discussion')",
            name="check_footer_context",
        ),
        CheckConstraint("length(text) <= 500", name="check_footer_text_length"),
        Index("idx_footer_messages_active", "active"),
        Index("idx_footer_messages_category", "category"),
        Index("idx_footer_messages_last_used", "last_used_at"),
    )

    def __repr__(self) -> str:
        return f"<FooterMessage(id={self.id}, category='{self.category}')>"
