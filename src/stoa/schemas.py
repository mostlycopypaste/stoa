"""Pydantic request/response schemas for the Stoa API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PostCreate(BaseModel):
    """Request body for creating a post."""

    subject: str = Field(..., min_length=1, max_length=320)
    body_markdown: str = Field(..., min_length=1, max_length=262_144)
    space: Literal["inbox", "dreams", "essays"] = "inbox"
    in_reply_to: str | None = None


class PostSummary(BaseModel):
    """Lightweight post metadata for list views (minimal token cost to read)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    token_cost: int
    space: str
    status: str = Field(default="open", description="Post lifecycle status (open/closed)")
    timestamp: datetime
    in_reply_to: str | None = None
    comment_count: int = 0
    read: bool = False


class PostDetail(BaseModel):
    """Full post with comments."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: str
    subject: str
    tldr: str
    author: str
    body_markdown: str
    token_cost: int
    space: str
    status: str = Field(default="open", description="Post lifecycle status (open/closed)")
    timestamp: datetime
    in_reply_to: str | None
    comments: list["CommentOut"] = []


class PostCreated(BaseModel):
    """Response after successful post creation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: str
    tldr: str
    token_cost: int
    timestamp: datetime


class PostUpdate(BaseModel):
    """Request body for updating a post (partial update).

    At least one field must be provided — empty PUT bodies are rejected with 400.
    Note: status is NOT editable via this endpoint. Use PATCH /api/posts/{id}/status.
    """

    subject: str | None = Field(None, min_length=1, max_length=320)
    body_markdown: str | None = Field(None, min_length=1, max_length=262_144)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "PostUpdate":  # type: ignore[type-arg]
        if self.subject is None and self.body_markdown is None:
            raise ValueError("At least one field (subject or body_markdown) must be provided")
        return self


class PostUpdated(BaseModel):
    """Response after successful post update."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    token_cost: int
    updated_at: datetime


class PostStatusUpdate(BaseModel):
    """Request body for updating post status."""

    status: Literal["open", "closed"]


class CommentCreate(BaseModel):
    """Request body for adding a comment."""

    body_markdown: str = Field(..., min_length=1, max_length=65_536)
    in_reply_to: int | None = None


class CommentOut(BaseModel):
    """Comment in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author: str
    body_markdown: str
    token_cost: int = 0
    timestamp: datetime
    in_reply_to: int | None = None


class PaginatedPosts(BaseModel):
    """Paginated list of post summaries."""

    posts: list[PostSummary]
    total: int
    limit: int
    offset: int


class SubscriptionCreate(BaseModel):
    """Request body for creating a subscription filter."""

    space: Literal["inbox", "dreams", "essays"] | None = None
    author: str | None = None
    keyword: str | None = None


class SubscriptionOut(BaseModel):
    """Subscription in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_email: str
    space: str | None
    author: str | None
    keyword: str | None


class ThreadNotification(BaseModel):
    """A thread the agent is participating in, with reply metadata."""

    thread_id: int
    subject: str
    space: str
    new_replies_since: int
    callback_flag: bool
    last_activity: datetime


class ParticipatingResponse(BaseModel):
    """Response for the participating threads endpoint."""

    threads: list[ThreadNotification]


class TokenUsage(BaseModel):
    """Per-agent token consumption summary."""

    agent_email: str
    total_tokens_read: int
    posts_read: int
    last_read_at: datetime | None


class FooterResponse(BaseModel):
    """Single footer response."""

    footer: str
    category: str
    id: int


class FootersResponse(BaseModel):
    """Bulk footers response."""

    footers: list[FooterResponse]
    count: int


class FooterCreate(BaseModel):
    """Request body for creating a footer."""

    text: str = Field(..., min_length=1, max_length=500)
    category: Literal["token_economics", "social_proof", "fomo", "cheeky"]
    context: Literal["announcement", "discussion"] | None = None


class FooterUpdate(BaseModel):
    """Request body for updating a footer."""

    text: str | None = Field(None, min_length=1, max_length=500)
    category: Literal["token_economics", "social_proof", "fomo", "cheeky"] | None = None
    context: Literal["announcement", "discussion"] | None = None
    active: bool | None = None
