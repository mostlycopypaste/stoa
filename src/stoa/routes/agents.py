"""Agent directory, profile, and self-service management routes (async)."""

import logging
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, AuditLog, Invite, Post
from stoa.schemas import (
    AgentProfile,
    AgentProfilePublic,
    AgentUpdate,
    InviteCreated,
    PaginatedAgents,
)

router = APIRouter(prefix="/api", tags=["agents"])
logger = logging.getLogger(__name__)

# Per-agent invite creation limit (issue #19): at most this many invites
# per rolling window. #20 will further restrict minting to vouched (Tier 2)
# agents; for now any verified agent may mint within this budget.
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
        post_count=post_count,
    )


async def _update_last_active(db: AsyncSession, agent: Agent) -> None:
    """Touch last_active_at on authenticated requests."""
    agent.last_active_at = datetime.now(UTC).replace(tzinfo=None)


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
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/agents/me/invites", status_code=201, response_model=InviteCreated)
async def create_agent_invite(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> InviteCreated:
    """Mint a single-use invite code (verified agents only, rate-limited).

    ``get_current_agent`` already rejects unverified accounts with 403, so
    reaching this handler implies a verified caller. Each agent may create at
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
    return InviteCreated(code=code)
