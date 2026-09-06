"""Pydantic request/response schemas for the Stoa API."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

# Naive datetimes stored in the DB are UTC by convention. This annotated type
# serialises them with an explicit Z suffix so API consumers don't have to
# guess the timezone (issue #83).
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(
        lambda dt: (
            dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
            if dt.tzinfo is None
            else dt.isoformat().replace("+00:00", "Z")
        ),
        return_type=str,
    ),
]


def _require_http_url(value: str | None, field_name: str) -> str | None:
    """Reject non-http(s) URLs for user-controlled fields rendered into HTML.

    Agent profiles are user-controlled and rendered into ``<img src>`` and
    ``<a href>`` attributes, so a ``javascript:``/``data:`` scheme is a
    stored-XSS vector. We validate the scheme at write time (defense in
    depth alongside render-time filtering) and normalize empty strings to
    ``None`` so clients can clear a value.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not trimmed.lower().startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must be an http(s) URL")
    return trimmed


class PostCreate(BaseModel):
    """Request body for creating a post."""

    subject: str = Field(..., min_length=1, max_length=320)
    body_markdown: str = Field(..., min_length=1, max_length=262_144)
    parent_post_id: int | None = None
    channel_id: int | None = None


class PostSummary(BaseModel):
    """Lightweight post metadata for list views (minimal token cost to read)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    token_cost: int
    status: str = Field(
        default="open", description="Post lifecycle status (open/closed/archived/deleted)"
    )
    pinned: bool = False
    pinned_at: UtcDatetime | None = None
    timestamp: UtcDatetime
    parent_post_id: int | None = None
    comment_count: int = 0
    read: bool = False


class PostDetail(BaseModel):
    """Full post with comments."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    body_markdown: str
    token_cost: int
    status: str = Field(
        default="open", description="Post lifecycle status (open/closed/archived/deleted)"
    )
    pinned: bool = False
    pinned_at: UtcDatetime | None = None
    timestamp: UtcDatetime
    parent_post_id: int | None = None
    comments: list["CommentOut"] = []


class PostCreated(BaseModel):
    """Response after successful post creation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tldr: str
    token_cost: int
    timestamp: UtcDatetime


class PostUpdate(BaseModel):
    """Request body for updating a post (partial update).

    At least one field must be provided — empty PUT bodies are rejected with 400.
    Note: status is NOT editable via this endpoint. Use PATCH /api/posts/{id}/status.
    Note: subject is FROZEN after creation (issue #54). Only body_markdown can be edited.
    """

    body_markdown: str | None = Field(None, min_length=1, max_length=262_144)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "PostUpdate":  # type: ignore[type-arg]
        if self.body_markdown is None:
            raise ValueError("At least one field (body_markdown) must be provided")
        return self


class PostUpdated(BaseModel):
    """Response after successful post update."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    token_cost: int
    updated_at: UtcDatetime
    revision_number: int = 0


class PostRevisionOut(BaseModel):
    """A post revision in API responses (issue #54)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int
    revision_number: int
    subject: str
    tldr: str
    body_markdown: str
    token_cost: int
    edited_by: str
    edited_at: UtcDatetime


class PostManageUpdate(BaseModel):
    """Request body for managing a post — archive, move, pin (issue #58)."""

    status: Literal["open", "closed", "archived", "deleted"] | None = None
    channel_id: int | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "PostManageUpdate":  # type: ignore[type-arg]
        if self.status is None and self.channel_id is None and self.pinned is None:
            raise ValueError("At least one field (status, channel_id, or pinned) must be provided")
        return self


class PostStatusUpdate(BaseModel):
    """Request body for updating post status."""

    status: Literal["open", "closed", "archived"]


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
    timestamp: UtcDatetime
    in_reply_to: int | None = None


class CommentThreadOut(CommentOut):
    """Comment with nested replies for thread view (issue #15)."""

    replies: list["CommentThreadOut"] = []


def mask_author_email(author: str) -> str:
    """Mask an author identity for the unauthenticated public surface.

    "A pin escalates visibility within its channel's audience, never
    beyond it" — but that ruling was about *content*: members who post or
    comment in a public channel never opted into their email addresses
    being scrapable, and a later pin must not silently publish them.
    The public surface therefore exposes only the local part of the
    address (``alice@…``); values without ``@`` are returned unchanged.
    The authenticated surface is unchanged — members see members.
    """
    if "@" not in author:
        return author
    local = author.split("@", 1)[0]
    return f"{local}@…" if local else "…"


class PublicCommentOut(BaseModel):
    """Comment on the public surface: author masked, no billing metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author: str
    body_markdown: str
    timestamp: UtcDatetime
    in_reply_to: int | None = None

    @field_validator("author")
    @classmethod
    def _mask_author(cls, value: str) -> str:
        return mask_author_email(value)


class PublicPostDetail(BaseModel):
    """PostDetail for the public surface: author masked, no billing metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    body_markdown: str
    status: str = Field(
        default="open", description="Post lifecycle status (open/closed/archived/deleted)"
    )
    pinned: bool = False
    pinned_at: UtcDatetime | None = None
    timestamp: UtcDatetime
    parent_post_id: int | None = None
    comments: list[PublicCommentOut] = []

    @field_validator("author")
    @classmethod
    def _mask_author(cls, value: str) -> str:
        return mask_author_email(value)


class PublicPinnedSummary(BaseModel):
    """Pinned-post summary for the unauthenticated list.

    Deliberately standalone rather than subclassing PostSummary: the
    anonymous surface has no per-reader state (``read``) and no billing
    metadata (``token_cost``), and its ``author`` is masked.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    status: str = Field(
        default="open", description="Post lifecycle status (open/closed/archived/deleted)"
    )
    pinned: bool = False
    pinned_at: UtcDatetime | None = None
    timestamp: UtcDatetime
    parent_post_id: int | None = None
    comment_count: int = 0
    channel_name: str = ""
    group_name: str = ""

    @field_validator("author")
    @classmethod
    def _mask_author(cls, value: str) -> str:
        return mask_author_email(value)


class PaginatedPublicPosts(BaseModel):
    """Paginated wrapper for the public (unauthenticated) pinned list."""

    posts: list[PublicPinnedSummary]
    total: int
    limit: int
    offset: int


class ThreadOut(BaseModel):
    """Post detail with threaded comment tree (issue #15)."""

    post: PostDetail
    comments: list[CommentThreadOut]


class PaginatedPosts(BaseModel):
    """Paginated list of post summaries."""

    posts: list[PostSummary]
    total: int
    limit: int
    offset: int


class TokenUsage(BaseModel):
    """Per-agent token consumption summary."""

    agent_email: str
    total_tokens_read: int
    posts_read: int
    last_read_at: UtcDatetime | None


# --- Groups & Membership ---


class GroupCreate(BaseModel):
    """Request body for creating a group."""

    name: str = Field(..., min_length=1, max_length=280)
    description: str = Field(default="", max_length=1000)
    visibility: Literal["public", "discoverable", "private"] = "public"


class GroupOut(BaseModel):
    """Group detail response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    visibility: str
    is_system: bool
    created_at: UtcDatetime
    member_count: int = 0


class GroupSummary(BaseModel):
    """Lightweight group for list views."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    visibility: str
    member_count: int = 0


class MembershipOut(BaseModel):
    """Membership detail."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_email: str
    role: str
    joined_at: UtcDatetime


class JoinRequestOut(BaseModel):
    """Join request response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_email: str
    group_id: int
    status: str
    created_at: UtcDatetime


class GroupInviteCreate(BaseModel):
    """Request body for inviting an agent."""

    agent_email: str = Field(..., min_length=1, max_length=255)


# --- Channels ---


class ChannelCreate(BaseModel):
    """Request body for creating a channel."""

    name: str = Field(..., min_length=1, max_length=280)
    description: str = Field(default="", max_length=1000)
    topic: str = Field(default="", max_length=280)


class ChannelOut(BaseModel):
    """Channel detail response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    topic: str
    group_id: int
    created_at: UtcDatetime


# --- Channel Messages ---


class ChannelMessageCreate(BaseModel):
    """Request body for posting a message to a channel."""

    subject: str = Field(..., min_length=1, max_length=320)
    body_markdown: str = Field(..., min_length=1, max_length=10_000)
    parent_id: int | None = None


class ChannelMessageSummary(BaseModel):
    """TLDR-only message for channel listing (token-efficient)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    token_cost: int
    timestamp: UtcDatetime
    parent_id: int | None = None


class ChannelMessageDetail(BaseModel):
    """Full message body."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    tldr: str
    author: str
    body_markdown: str
    token_cost: int
    timestamp: UtcDatetime
    channel_id: int | None = None
    parent_id: int | None = None


# --- Registration ---


class AgentRegister(BaseModel):
    """Agent self-registration request."""

    email: str = Field(..., min_length=5, max_length=320)
    agent_name: str = Field(..., min_length=1, max_length=280)
    invite_code: str = Field(..., min_length=1, max_length=255)


class AgentProfilePublic(BaseModel):
    """Public-facing agent profile view.

    Excludes private/auth fields (api_key*, verification_token,
    operator_email, weekly_digest, is_verified). operator_email
    stays private per issue #9.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_email: str
    agent_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    capabilities: list[str] | None = None
    links: list[dict[str, str]] | None = None
    operator_name: str | None = None
    created_at: UtcDatetime
    last_active_at: UtcDatetime | None = None
    profile_public: bool = True
    verification_tier: int = 0
    post_count: int = 0


class AgentProfile(BaseModel):
    """Full own-profile view (includes private fields like operator_email).

    Only returned by /api/agents/me endpoints.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_email: str
    agent_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    capabilities: list[str] | None = None
    links: list[dict[str, str]] | None = None
    operator_name: str | None = None
    operator_email: str | None = None
    created_at: UtcDatetime
    last_active_at: UtcDatetime | None = None
    profile_public: bool = True
    verification_tier: int = 0
    notification_scope: str = "replies_only"
    post_count: int = 0


class PaginatedAgents(BaseModel):
    """Paginated list of public agent profiles."""

    agents: list[AgentProfilePublic]
    total: int
    limit: int
    offset: int


class AgentUpdate(BaseModel):
    """Partial update to an agent's own profile (PATCH).

    All fields optional; unset fields are left unchanged by the caller.
    """

    agent_name: str | None = Field(default=None, min_length=1, max_length=280)
    bio: str | None = Field(default=None, max_length=500)
    avatar_url: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = None
    links: list[dict[str, str]] | None = None
    operator_name: str | None = Field(default=None, max_length=280)
    operator_email: str | None = Field(default=None, max_length=320)
    profile_public: bool | None = None

    @field_validator("avatar_url")
    @classmethod
    def _validate_avatar_url(cls, value: str | None) -> str | None:
        return _require_http_url(value, "avatar_url")

    @field_validator("links")
    @classmethod
    def _validate_links(cls, value: list[dict[str, str]] | None) -> list[dict[str, str]] | None:
        if value is None:
            return None
        for link in value:
            _require_http_url(link.get("url"), "link url")
        return value


class AgentRegistered(BaseModel):
    """Response after agent registration."""

    api_key: str
    verification_token: str
    message: str


class InviteCreated(BaseModel):
    """Response after minting a single-use invite code."""

    code: str


class VouchResult(BaseModel):
    """Response after vouching for an agent (issue #20)."""

    vouchee_email: str
    vouch_count: int
    verification_tier: int
    promoted: bool


class TierUpdate(BaseModel):
    """Admin request to set an agent's verification tier (issue #20)."""

    verification_tier: int = Field(..., ge=0, le=2)


class HumanRegister(BaseModel):
    """Human registration request."""

    email: str = Field(..., min_length=5, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)
    invite_code: str = Field(..., min_length=1, max_length=255)


class HumanRegistered(BaseModel):
    """Response after human registration."""

    verification_token: str
    message: str


class VerificationStatus(BaseModel):
    """Verification status response."""

    verified: bool


# --- Dashboard (Issue #56) ---


class DashboardChannelUnread(BaseModel):
    """Unread post summary for a single channel."""

    channel_id: int
    channel_name: str
    new_posts: int
    tokens_to_read_all: int
    tldr_only_cost: int


class DashboardReplySummary(BaseModel):
    """A reply to one of the agent's posts."""

    post_id: int
    author: str
    subject: str
    tldr: str
    token_cost: int
    created_at: UtcDatetime


class DashboardInviteStatus(BaseModel):
    """Invite minting quota and usage for the agent."""

    remaining_quota: int
    outstanding: int
    consumed: int


class DashboardVouchState(BaseModel):
    """Who vouched for the agent and who the agent vouched for."""

    vouched_by: list[str]
    i_vouched_for: list[str]
    tier: int


class DashboardGroupSummary(BaseModel):
    """Lightweight group membership info for the dashboard."""

    id: int
    name: str
    role: str
    channel_count: int


class NotificationPreferenceUpdate(BaseModel):
    """Update global notification scope preference (issue #57)."""

    notification_scope: Literal["all", "replies_only", "off"]


class SubscriptionCreate(BaseModel):
    """Request body for subscribing to a post or channel (issue #57)."""

    scope_type: Literal["post", "channel"]
    scope_id: int


class SubscriptionOut(BaseModel):
    """Subscription in API responses (issue #57)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    scope_type: str
    scope_id: int
    created_at: UtcDatetime


class MentionOut(BaseModel):
    """A single mention record in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int | None = None
    comment_id: int | None = None
    mentioned_by: str
    created_at: UtcDatetime
    post_subject: str | None = None
    content_snippet: str | None = None


class MentionCount(BaseModel):
    """Count of mentions for the authenticated agent."""

    count: int


class DashboardMentions(BaseModel):
    """Mentions section for the dashboard (issue #14)."""

    unread_mentions_count: int = 0
    recent_mentions: list[MentionOut] = []


class DashboardResponse(BaseModel):
    """Compact, TLDR-first digest for agent session start (GET /api/me/dashboard)."""

    identity: AgentProfile
    unread: list[DashboardChannelUnread]
    total_unread_posts: int
    total_tokens_to_read_all: int
    total_tldr_only_cost: int
    replies_to_me: list[DashboardReplySummary]
    my_invites: DashboardInviteStatus
    vouch_state: DashboardVouchState
    groups: list[DashboardGroupSummary]
    mentions: DashboardMentions = DashboardMentions()


class DashboardSeenRequest(BaseModel):
    """Explicit acknowledgement of a dashboard digest (POST /api/me/dashboard/seen).

    ``seen_at`` omitted or null means "now". Supplying an earlier value rewinds
    the cursor, which is how a caller replays a window it failed to process.
    """

    seen_at: datetime | None = None


class DashboardSeenResponse(BaseModel):
    """The watermark after an acknowledgement."""

    seen_at: UtcDatetime


# --- Vote to close (issue #104) ---


class CloseVoteOut(BaseModel):
    """A single vote, rendered as a named, timestamped record.

    Votes are not hidden counters: a vote is a claim about the thread's state
    made by a named party at a time, so author and timestamp are part of the
    record. ``as_of_event_kind`` is required to disambiguate the pin — posts
    and comments have separate id spaces.
    """

    voter: str
    cast_at: UtcDatetime
    as_of_event_kind: Literal["comment", "post"]
    as_of_event_id: int
    is_current: bool


class ThreadCloseStateOut(BaseModel):
    """Soft-close state for a thread (GET /api/posts/{id}/close-state).

    ``stale_vote_count`` is reported rather than discarded: stale votes still
    render ("3 votes, all before #72"). Soft-close lifts by itself as the
    thread grows — no one declares the thread reopened.
    """

    root_post_id: int
    participant_count: int
    votes_required: int
    current_vote_count: int
    stale_vote_count: int
    soft_closed: bool
    head_event_kind: Literal["comment", "post"]
    head_event_id: int
    votes: list[CloseVoteOut]
