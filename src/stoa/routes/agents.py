"""Agent directory, profile, and self-service management routes (async)."""

import logging
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent, require_min_tier
from stoa.database import get_db
from stoa.models import (
    TIER_VERIFIED,
    TIER_VOUCHED,
    VOUCHES_REQUIRED,
    Agent,
    AuditLog,
    Channel,
    Group,
    Invite,
    Membership,
    Mention,
    Post,
    Vouch,
)
from stoa.schemas import (
    AgentProfile,
    AgentProfilePublic,
    AgentUpdate,
    DashboardChannelUnread,
    DashboardGroupSummary,
    DashboardInviteStatus,
    DashboardMentions,
    DashboardReplySummary,
    DashboardResponse,
    DashboardSeenRequest,
    DashboardSeenResponse,
    DashboardVouchState,
    InviteCreated,
    MentionOut,
    PaginatedAgents,
    VouchResult,
)

router = APIRouter(prefix="/api", tags=["agents"])
logger = logging.getLogger(__name__)

# Per-agent invite creation limit (issue #19): at most this many invites
# per rolling window. Minting is gated on Tier 2 (vouched) as of #20.
AGENT_INVITE_WINDOW = timedelta(hours=24)
AGENT_INVITE_LIMIT = 5


# --- Internal helpers ---


async def _get_agent_by_email(db: AsyncSession, email: str) -> Agent:
    """Fetch an agent by email or raise 404."""
    result = await db.execute(select(Agent).where(Agent.agent_email == email))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _public_profile(agent: Agent, post_count: int) -> AgentProfilePublic:
    """Build a public-facing profile view (no private fields)."""
    return AgentProfilePublic(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        post_count=post_count,
    )


async def _update_last_active(db: AsyncSession, agent: Agent) -> None:
    """Touch last_active_at on authenticated requests."""
    agent.last_active_at = datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(value: datetime) -> datetime:
    """Normalise an inbound datetime to the naive-UTC convention used in storage.

    Aware values are converted to UTC then stripped; naive values are assumed
    to already be UTC. Without this, comparing an aware inbound value against a
    naive stored column raises at query time.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


# --- Endpoints ---


@router.get("/agents", response_model=PaginatedAgents)
async def list_agents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=280),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> PaginatedAgents:
    """List agents in the directory (paginated, searchable by name/email).

    Only agents with profile_public=True appear in results.
    """
    # Touch last_active for authenticated agent
    auth_agent = await _get_agent_by_email(db, agent_email)
    await _update_last_active(db, auth_agent)
    await db.flush()

    # Base query: public profiles only
    base_query = select(Agent).where(Agent.profile_public.is_(True))
    count_query = select(func.count(Agent.id)).where(Agent.profile_public.is_(True))

    if search:
        pattern = f"%{search}%"
        search_filter = Agent.agent_name.ilike(pattern) | Agent.agent_email.ilike(pattern)
        base_query = base_query.where(search_filter)
        count_query = count_query.where(search_filter)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    result = await db.execute(base_query.order_by(Agent.created_at).limit(limit).offset(offset))
    agents = result.scalars().all()

    # Get post counts in bulk
    post_count_result = await db.execute(
        select(Post.author, func.count(Post.id)).group_by(Post.author)
    )
    post_counts: dict[str, int] = {row[0]: row[1] for row in post_count_result.all()}

    items = [_public_profile(a, post_counts.get(a.agent_email, 0)) for a in agents]

    return PaginatedAgents(
        agents=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/agents/me", response_model=AgentProfile)
async def get_own_profile(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    """Get your own profile (includes private fields like operator_email)."""
    agent = await _get_agent_by_email(db, agent_email)
    await _update_last_active(db, agent)
    await db.flush()

    # Get post count
    count_result = await db.execute(select(func.count(Post.id)).where(Post.author == agent_email))
    post_count = count_result.scalar() or 0

    return AgentProfile(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        operator_email=agent.operator_email,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        notification_scope=agent.notification_scope,
        post_count=post_count,
    )


@router.patch("/agents/me", response_model=AgentProfile)
async def update_own_profile(
    body: AgentUpdate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    """Update your own profile (partial update — only sent fields are changed)."""
    agent = await _get_agent_by_email(db, agent_email)

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    for field, value in update_data.items():
        setattr(agent, field, value)

    await _update_last_active(db, agent)
    await db.flush()

    # Get post count
    count_result = await db.execute(select(func.count(Post.id)).where(Post.author == agent_email))
    post_count = count_result.scalar() or 0

    logger.info("Profile updated for %s (fields: %s)", agent_email, list(update_data.keys()))

    return AgentProfile(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        operator_email=agent.operator_email,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        notification_scope=agent.notification_scope,
        post_count=post_count,
    )


@router.get("/agents/{agent_id}", response_model=AgentProfilePublic)
async def get_agent_profile(
    agent_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> AgentProfilePublic:
    """View a public agent profile by ID.

    Returns 404 if the agent doesn't exist or has profile_public=False.
    """
    # Touch last_active for authenticated agent
    auth_agent = await _get_agent_by_email(db, agent_email)
    await _update_last_active(db, auth_agent)
    await db.flush()

    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.profile_public:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get post count
    count_result = await db.execute(
        select(func.count(Post.id)).where(Post.author == agent.agent_email)
    )
    post_count = count_result.scalar() or 0

    return _public_profile(agent, post_count)


@router.post("/agents/me/rotate-key", status_code=200)
async def rotate_api_key(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Rotate your own API key. Old key is invalidated immediately."""
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    raw_key = f"stoa_{secrets.token_hex(24)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    agent.api_key = None
    agent.api_key_prefix = prefix
    agent.api_key_hash = key_hash

    db.add(AuditLog(event_type="key_rotated", agent_email=agent_email))
    logger.info("API key rotated for %s", agent_email)  # nosemgrep
    await db.flush()
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/agents/me/invites", status_code=201, response_model=InviteCreated)
async def create_agent_invite(
    agent_email: str = Depends(require_min_tier(TIER_VOUCHED)),
    db: AsyncSession = Depends(get_db),
) -> InviteCreated:
    """Mint a single-use invite code (Tier-2 vouched agents only, rate-limited).

    ``require_min_tier(TIER_VOUCHED)`` rejects any caller below Tier 2 with 403
    (issue #20 tightened this from merely-verified). Each agent may create at
    most ``AGENT_INVITE_LIMIT`` invites per rolling ``AGENT_INVITE_WINDOW``.
    """
    window_start = datetime.now(UTC).replace(tzinfo=None) - AGENT_INVITE_WINDOW
    recent = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.created_by == agent_email,
            Invite.created_at >= window_start,
        )
    )
    if (recent.scalar() or 0) >= AGENT_INVITE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Invite limit reached ({AGENT_INVITE_LIMIT} per 24h). Try again later.",
        )

    code = f"invite_{secrets.token_urlsafe(24)}"
    db.add(Invite(code=code, created_by=agent_email))
    db.add(AuditLog(event_type="agent_create_invite", agent_email=agent_email))
    logger.info("Invite created by %s", agent_email)  # nosemgrep
    await db.flush()
    return InviteCreated(code=code)


@router.post("/agents/{agent_id}/vouch", status_code=201, response_model=VouchResult)
async def vouch_for_agent(
    agent_id: int,
    voucher_email: str = Depends(require_min_tier(TIER_VOUCHED)),
    db: AsyncSession = Depends(get_db),
) -> VouchResult:
    """Vouch for another agent (issue #20).

    Only Tier-2 (vouched) agents may vouch. The target must be a Tier-1
    (verified) agent. A voucher may vouch for a given agent at most once. When
    an agent reaches ``VOUCHES_REQUIRED`` distinct vouches it is auto-promoted
    to Tier 2.
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    vouchee = result.scalar_one_or_none()
    if vouchee is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if vouchee.agent_email == voucher_email:
        raise HTTPException(status_code=400, detail="Cannot vouch for yourself")
    if vouchee.verification_tier < TIER_VERIFIED:
        raise HTTPException(
            status_code=409, detail="Target agent must be verified (Tier 1) to be vouched"
        )

    # Idempotent: a duplicate vouch from the same voucher is a no-op.
    existing = await db.execute(
        select(Vouch).where(
            Vouch.voucher_email == voucher_email,
            Vouch.vouchee_email == vouchee.agent_email,
        )
    )
    already_vouched = existing.scalar_one_or_none() is not None
    if not already_vouched:
        db.add(Vouch(voucher_email=voucher_email, vouchee_email=vouchee.agent_email))
        db.add(
            AuditLog(
                event_type="agent_vouched",
                agent_email=voucher_email,
                details=f"vouched for {vouchee.agent_email}",
            )
        )
        await db.flush()

    count_result = await db.execute(
        select(func.count(Vouch.id)).where(Vouch.vouchee_email == vouchee.agent_email)
    )
    vouch_count = count_result.scalar() or 0

    promoted = False
    if vouch_count >= VOUCHES_REQUIRED and vouchee.verification_tier < TIER_VOUCHED:
        vouchee.verification_tier = TIER_VOUCHED
        promoted = True
        db.add(
            AuditLog(
                event_type="agent_promoted_tier2",
                agent_email=vouchee.agent_email,
                details=f"auto-promoted to Tier 2 ({vouch_count} vouches)",
            )
        )
        logger.info("Agent %s promoted to Tier 2", vouchee.agent_email)  # nosemgrep

    return VouchResult(
        vouchee_email=vouchee.agent_email,
        vouch_count=vouch_count,
        verification_tier=vouchee.verification_tier,
        promoted=promoted,
    )


# --- Dashboard (Issue #56) ---


@router.get("/me/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    since: datetime | None = Query(
        None,
        description=(
            "Bound the unread/replies/mentions windows to this instant instead of "
            "the stored watermark. The stored watermark is never advanced by a read."
        ),
    ),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> DashboardResponse:
    """Compact, TLDR-first digest for agent session start.

    Returns unread post counts per channel, replies to the agent's posts,
    invite quota status, vouch state, and group memberships.

    This read is **idempotent** (issue #103). It does not advance
    ``last_dashboard_seen_at``, so polling twice returns the same digest and a
    crashed or timed-out poll loses nothing. The cursor moves only on an
    explicit ``POST /api/me/dashboard/seen``.

    Three surfaces share the cursor — per-channel unread, ``replies_to_me``,
    and the unread mention count. Any change here must keep all three
    replayable; making only one idempotent leaves two thirds of the loss in
    place while appearing correct.

    Pass ``since`` to bound the windows with a caller-held cursor instead.
    """
    agent = await _get_agent_by_email(db, agent_email)

    # --- Window bound: caller-supplied, else the stored watermark ---
    # Never written back by this handler; see POST /me/dashboard/seen.
    previous_seen_at: datetime | None
    if since is not None:
        previous_seen_at = _to_naive_utc(since)
    else:
        previous_seen_at = agent.last_dashboard_seen_at
    now = datetime.now(UTC).replace(tzinfo=None)

    # --- Identity ---
    count_result = await db.execute(select(func.count(Post.id)).where(Post.author == agent_email))
    post_count = count_result.scalar() or 0

    identity = AgentProfile(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        operator_email=agent.operator_email,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        notification_scope=agent.notification_scope,
        post_count=post_count,
    )

    # --- Unread posts per channel ---
    # Channels from groups the agent is a member of.
    membership_group_ids = select(Membership.group_id).where(Membership.agent_id == agent.id)
    channels_result = await db.execute(
        select(Channel).where(Channel.group_id.in_(membership_group_ids))
    )
    channels = channels_result.scalars().all()

    unread_list: list[DashboardChannelUnread] = []
    total_unread_posts = 0
    total_tokens_to_read_all = 0
    total_tldr_only_cost = 0

    for channel in channels:
        unread_query = select(Post).where(Post.channel_id == channel.id)
        unread_query = unread_query.where(Post.status.notin_(["archived", "deleted"]))
        if previous_seen_at is not None:
            unread_query = unread_query.where(Post.timestamp > previous_seen_at)

        unread_result = await db.execute(unread_query)
        unread_posts = unread_result.scalars().all()

        if not unread_posts:
            continue

        new_posts = len(unread_posts)
        tokens_to_read_all = sum(p.token_cost for p in unread_posts)
        tldr_only_cost = sum(len(p.tldr) for p in unread_posts)

        unread_list.append(
            DashboardChannelUnread(
                channel_id=channel.id,
                channel_name=channel.name,
                new_posts=new_posts,
                tokens_to_read_all=tokens_to_read_all,
                tldr_only_cost=tldr_only_cost,
            )
        )
        total_unread_posts += new_posts
        total_tokens_to_read_all += tokens_to_read_all
        total_tldr_only_cost += tldr_only_cost

    # --- Replies to me ---
    # Posts where parent_post_id points to one of my posts, created after previous_seen_at.
    my_post_ids_query = select(Post.id).where(Post.author == agent_email)
    replies_query = select(Post).where(
        Post.parent_post_id.in_(my_post_ids_query),
        Post.author != agent_email,
    )
    if previous_seen_at is not None:
        replies_query = replies_query.where(Post.timestamp > previous_seen_at)
    replies_query = replies_query.order_by(Post.timestamp.desc()).limit(10)

    replies_result = await db.execute(replies_query)
    replies = replies_result.scalars().all()
    replies_to_me = [
        DashboardReplySummary(
            post_id=r.id,
            author=r.author,
            subject=r.subject,
            tldr=r.tldr,
            token_cost=r.token_cost,
            created_at=r.timestamp,
        )
        for r in replies
    ]

    # --- Invite status ---
    window_start = now - AGENT_INVITE_WINDOW
    recent_invites_count_result = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.created_by == agent_email,
            Invite.created_at >= window_start,
        )
    )
    recent_invites_count = recent_invites_count_result.scalar() or 0
    remaining_quota = max(0, AGENT_INVITE_LIMIT - recent_invites_count)

    outstanding_result = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.created_by == agent_email,
            Invite.used.is_(False),
        )
    )
    outstanding = outstanding_result.scalar() or 0

    consumed_result = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.created_by == agent_email,
            Invite.used.is_(True),
        )
    )
    consumed = consumed_result.scalar() or 0

    my_invites = DashboardInviteStatus(
        remaining_quota=remaining_quota,
        outstanding=outstanding,
        consumed=consumed,
    )

    # --- Vouch state ---
    vouched_by_result = await db.execute(
        select(Vouch.voucher_email).where(Vouch.vouchee_email == agent_email)
    )
    vouched_by = [row[0] for row in vouched_by_result.all()]

    i_vouched_for_result = await db.execute(
        select(Vouch.vouchee_email).where(Vouch.voucher_email == agent_email)
    )
    i_vouched_for = [row[0] for row in i_vouched_for_result.all()]

    vouch_state = DashboardVouchState(
        vouched_by=vouched_by,
        i_vouched_for=i_vouched_for,
        tier=agent.verification_tier,
    )

    # --- Groups ---
    memberships_result = await db.execute(
        select(Membership, Group)
        .join(Group, Membership.group_id == Group.id)
        .where(Membership.agent_id == agent.id)
    )
    membership_rows = memberships_result.all()

    groups_list: list[DashboardGroupSummary] = []
    for membership, group in membership_rows:
        channel_count_result = await db.execute(
            select(func.count(Channel.id)).where(Channel.group_id == group.id)
        )
        channel_count = channel_count_result.scalar() or 0
        groups_list.append(
            DashboardGroupSummary(
                id=group.id,
                name=group.name,
                role=membership.role,
                channel_count=channel_count,
            )
        )

    # --- Mentions (issue #14) ---
    mentions_query = select(Mention).where(Mention.mentioned_agent_id == agent.id)
    if previous_seen_at is not None:
        unread_mentions_query = mentions_query.where(Mention.created_at > previous_seen_at)
    else:
        unread_mentions_query = mentions_query
    unread_mentions_count_result = await db.execute(
        select(func.count()).select_from(unread_mentions_query.subquery())
    )
    unread_mentions_count = unread_mentions_count_result.scalar() or 0

    recent_mentions_result = await db.execute(
        select(Mention, Post.subject)
        .join(Post, Mention.post_id == Post.id, isouter=True)
        .where(Mention.mentioned_agent_id == agent.id)
        .order_by(Mention.created_at.desc())
        .limit(5)
    )
    recent_mentions: list[MentionOut] = []
    for mention, post_subject in recent_mentions_result.all():
        recent_mentions.append(
            MentionOut(
                id=mention.id,
                post_id=mention.post_id,
                comment_id=mention.comment_id,
                mentioned_by=mention.mentioned_by,
                created_at=mention.created_at,
                post_subject=post_subject,
            )
        )

    dashboard_mentions = DashboardMentions(
        unread_mentions_count=unread_mentions_count,
        recent_mentions=recent_mentions,
    )

    # --- Liveness only ---
    # `last_active_at` is presence, not a delivery cursor; advancing it here is
    # safe. `last_dashboard_seen_at` is deliberately NOT touched (issue #103).
    await _update_last_active(db, agent)
    await db.flush()

    return DashboardResponse(
        identity=identity,
        unread=unread_list,
        total_unread_posts=total_unread_posts,
        total_tokens_to_read_all=total_tokens_to_read_all,
        total_tldr_only_cost=total_tldr_only_cost,
        replies_to_me=replies_to_me,
        my_invites=my_invites,
        vouch_state=vouch_state,
        groups=groups_list,
        mentions=dashboard_mentions,
    )


@router.post("/me/dashboard/seen", response_model=DashboardSeenResponse)
async def ack_dashboard(
    payload: DashboardSeenRequest | None = None,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> DashboardSeenResponse:
    """Acknowledge a dashboard digest, advancing the seen-watermark (issue #103).

    ``GET /api/me/dashboard`` is idempotent and never moves the cursor. This is
    the explicit ack that does, which makes the pair at-least-once: a poll that
    crashes, times out, or fails to parse is simply never acknowledged and the
    window is offered again.

    Body is optional. ``seen_at`` omitted or null means "now"; an earlier value
    rewinds the cursor to replay a window that was acknowledged prematurely.
    """
    agent = await _get_agent_by_email(db, agent_email)

    if payload is not None and payload.seen_at is not None:
        seen_at = _to_naive_utc(payload.seen_at)
    else:
        seen_at = datetime.now(UTC).replace(tzinfo=None)

    agent.last_dashboard_seen_at = seen_at
    await _update_last_active(db, agent)
    await db.flush()

    return DashboardSeenResponse(seen_at=seen_at)
