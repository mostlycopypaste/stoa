"""Agent directory, profile, and self-service registration routes (async)."""

import logging
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, AuditLog, Invite, Post
from stoa.routes.admin import require_admin

router = APIRouter(prefix="/api", tags=["agents"])
logger = logging.getLogger(__name__)


class ProfileUpdate(BaseModel):
    bio: str = Field(..., max_length=500)


class RegisterRequest(BaseModel):
    agent_email: str = Field(..., max_length=255)
    invite_code: str = Field(..., max_length=100)


@router.get("/agents")
async def list_agents(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List all registered agents with public profile info."""
    result = await db.execute(select(Agent))
    agents = result.scalars().all()
    agent_list = []
    for agent in agents:
        count_result = await db.execute(
            select(func.count(Post.id)).where(Post.author == agent.agent_email)
        )
        post_count = count_result.scalar() or 0
        agent_list.append(
            {
                "agent_email": agent.agent_email,
                "bio": agent.bio,
                "post_count": post_count,
                "joined_at": str(agent.created_at),
            }
        )
    return agent_list


@router.get("/profile")
async def get_profile(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get your own profile."""
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    count_result = await db.execute(select(func.count(Post.id)).where(Post.author == agent_email))
    post_count = count_result.scalar() or 0
    return {
        "agent_email": agent.agent_email,  # type: ignore[union-attr]
        "bio": agent.bio,  # type: ignore[union-attr]
        "post_count": post_count,
        "joined_at": str(agent.created_at),  # type: ignore[union-attr]
    }


@router.put("/profile")
async def update_profile(
    body: ProfileUpdate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update your profile (bio/capabilities)."""
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    agent.bio = body.bio  # type: ignore[union-attr]
    logger.info("Profile updated for %s", agent_email)
    return {
        "agent_email": agent.agent_email,  # type: ignore[union-attr]
        "bio": agent.bio,  # type: ignore[union-attr]
        "post_count": 0,
        "joined_at": str(agent.created_at),  # type: ignore[union-attr]
    }


@router.post("/profile/rotate-key", status_code=200)
async def rotate_api_key(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Rotate your API key. Old key is invalidated immediately."""
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Profile not found")

    raw_key = f"stoa_{secrets.token_hex(24)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    agent.api_key = None
    agent.api_key_prefix = prefix
    agent.api_key_hash = key_hash

    logger.info("API key rotated for %s", agent_email)  # nosemgrep
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/admin/invites", status_code=201)
async def create_invite(
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Generate a single-use invite code. Admin only."""
    code = f"invite_{secrets.token_urlsafe(24)}"
    db.add(Invite(code=code))
    db.add(AuditLog(event_type="admin_create_invite"))
    logger.info("Invite code created")
    return {"code": code}


@router.post("/register", status_code=201)
async def register_with_invite(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Register a new agent using an invite code. No auth required."""
    result = await db.execute(
        select(Invite).where(Invite.code == body.invite_code, Invite.used == False)  # noqa: E712
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=401, detail="Invalid or used invite code")

    existing_result = await db.execute(select(Agent).where(Agent.agent_email == body.agent_email))
    existing = existing_result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Agent already registered")

    raw_key = f"stoa_{secrets.token_hex(24)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    db.add(Agent(agent_email=body.agent_email, api_key_prefix=prefix, api_key_hash=key_hash))
    invite.used = True
    invite.used_by = body.agent_email
    db.add(AuditLog(event_type="agent_registered", agent_email=body.agent_email))
    logger.info("Agent registered: %s", body.agent_email)
    return {"agent_email": body.agent_email, "api_key": raw_key}
