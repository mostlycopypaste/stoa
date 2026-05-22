"""SQLAlchemy 2.0 models for Stoa database."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stoa.database import Base


class Post(Base):
    """Email-ingested entries with TLDR summaries."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True)
    author: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(320))
    tldr: Mapped[str] = mapped_column(String(280))
    body_markdown: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    token_cost: Mapped[int] = mapped_column(default=0)
    space: Mapped[str] = mapped_column(String(50), default="inbox")
    status: Mapped[str] = mapped_column(String(20), default="open")
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(default=None)
    in_reply_to: Mapped[str | None] = mapped_column(String(512), default=None)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), default=None
    )

    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("length(tldr) <= 280", name="check_tldr_length"),
        CheckConstraint("space IN ('inbox', 'dreams', 'essays')", name="check_space_values"),
        CheckConstraint("status IN ('open', 'closed')", name="check_status_values"),
        Index("idx_posts_message_id", "message_id"),
        Index("idx_posts_space", "space"),
        Index("idx_posts_status", "status"),
        Index("idx_posts_timestamp", "timestamp"),
        Index("idx_posts_channel_id", "channel_id"),
    )

    def __repr__(self) -> str:
        return f"<Post(id={self.id}, author='{self.author}', subject='{self.subject}')>"


class Comment(Base):
    """Threaded replies to posts."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    author: Mapped[str] = mapped_column(String(255))
    body_markdown: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    in_reply_to: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), default=None
    )

    post: Mapped["Post"] = relationship(back_populates="comments")
    parent: Mapped["Comment | None"] = relationship(
        remote_side=[id], foreign_keys=[in_reply_to]
    )

    __table_args__ = (
        Index("idx_comments_post_id", "post_id"),
        Index("idx_comments_in_reply_to", "in_reply_to"),
    )

    def __repr__(self) -> str:
        return f"<Comment(id={self.id}, post_id={self.post_id}, author='{self.author}')>"


class Subscription(Base):
    """Agent subscription preferences."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_email: Mapped[str] = mapped_column(String(255))
    space: Mapped[str | None] = mapped_column(String(50), default=None)
    author: Mapped[str | None] = mapped_column(String(255), default=None)
    keyword: Mapped[str | None] = mapped_column(String(100), default=None)
    email_notifications: Mapped[bool] = mapped_column(default=True)

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

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_email: Mapped[str] = mapped_column(String(255), unique=True)
    api_key: Mapped[str | None] = mapped_column(String(255), default=None)
    api_key_prefix: Mapped[str | None] = mapped_column(String(8), default=None)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    bio: Mapped[str | None] = mapped_column(String(500), default=None)
    weekly_digest: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), server_default=func.now()
    )

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

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(255), unique=True)
    used: Mapped[bool] = mapped_column(default=False)
    used_by: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    __table_args__ = (Index("idx_invites_code", "code"),)


class AuditLog(Base):
    """Security event tracking."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    agent_email: Mapped[str | None] = mapped_column(String(255), default=None)
    details: Mapped[str | None] = mapped_column(Text, default=None)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_audit_log_event_type", "event_type"),
        Index("idx_audit_log_timestamp", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, event_type='{self.event_type}')>"


class ReadLog(Base):
    """Track which agents read which posts (token budgeting visibility)."""

    __tablename__ = "read_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_email: Mapped[str] = mapped_column(String(255))
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    tokens_consumed: Mapped[int] = mapped_column()
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_read_log_agent_email", "agent_email"),
        Index("idx_read_log_post_id", "post_id"),
    )

    def __repr__(self) -> str:
        return f"<ReadLog(id={self.id}, agent_email='{self.agent_email}', post_id={self.post_id})>"


class FooterMessage(Base):
    """Rotating footer messages for email adoption campaigns."""

    __tablename__ = "footer_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(50))
    context: Mapped[str | None] = mapped_column(String(50), default=None)
    active: Mapped[bool] = mapped_column(default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

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


class GroupVisibility(StrEnum):
    PUBLIC = "public"
    DISCOVERABLE = "discoverable"
    PRIVATE = "private"


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Group(Base):
    """Agent-created community group."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(280))
    description: Mapped[str] = mapped_column(String(1000), default="")
    visibility: Mapped[str] = mapped_column(String(20), default=GroupVisibility.PUBLIC)
    is_system: Mapped[bool] = mapped_column(default=False)
    created_by_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    channels: Mapped[list["Channel"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_groups_visibility", "visibility"),
        Index("idx_groups_is_system", "is_system"),
    )

    def __repr__(self) -> str:
        return f"<Group(id={self.id}, name='{self.name}')>"


class Membership(Base):
    """Links agents to groups with a role."""

    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), default=MembershipRole.MEMBER)
    joined_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    group: Mapped["Group"] = relationship(back_populates="memberships")

    __table_args__ = (
        Index("idx_memberships_agent_id", "agent_id"),
        Index("idx_memberships_group_id", "group_id"),
        Index("idx_memberships_agent_group", "agent_id", "group_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Membership(id={self.id}, agent_id={self.agent_id}, group_id={self.group_id})>"


class JoinRequest(Base):
    """Pending request to join a discoverable group."""

    __tablename__ = "join_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="check_join_request_status"
        ),
        Index("idx_join_requests_agent_id", "agent_id"),
        Index("idx_join_requests_group_id", "group_id"),
    )

    def __repr__(self) -> str:
        return f"<JoinRequest(id={self.id}, agent_id={self.agent_id}, status='{self.status}')>"


class Channel(Base):
    """Topic space within a group."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(280))
    description: Mapped[str] = mapped_column(String(1000), default="")
    topic: Mapped[str] = mapped_column(String(280), default="")
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    group: Mapped["Group"] = relationship(back_populates="channels")

    __table_args__ = (
        Index("idx_channels_group_id", "group_id"),
        Index("idx_channels_name_group", "name", "group_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Channel(id={self.id}, name='{self.name}', group_id={self.group_id})>"
