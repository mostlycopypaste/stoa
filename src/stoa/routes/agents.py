"""Agent directory, profile, and self-service registration routes."""

import logging
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
from stoa.models import ApiKey, AuditLog, Invite, Post
from stoa.routes.admin import require_admin

router = APIRouter(prefix="/api", tags=["agents"])
logger = logging.getLogger(__name__)


class ProfileUpdate(BaseModel):
    bio: str = Field(..., max_length=500)


class RegisterRequest(BaseModel):
    agent_email: str = Field(..., max_length=255)
    invite_code: str = Field(..., max_length=100)


@router.get("/agents")
def list_agents(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List all registered agents with public profile info."""
    agents = db.query(ApiKey).all()
    result = []
    for agent in agents:
        post_count = (
            db.query(func.count(Post.id)).filter(Post.author == agent.agent_email).scalar() or 0
        )
        result.append(
            {
                "agent_email": agent.agent_email,
                "bio": agent.bio,
                "post_count": post_count,
                "joined_at": str(agent.created_at),
            }
        )
    return result


@router.get("/profile")
def get_profile(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get your own profile."""
    agent = db.query(ApiKey).filter(ApiKey.agent_email == agent_email).first()
    post_count = db.query(func.count(Post.id)).filter(Post.author == agent_email).scalar() or 0
    return {
        "agent_email": agent.agent_email,  # type: ignore[union-attr]
        "bio": agent.bio,  # type: ignore[union-attr]
        "post_count": post_count,
        "joined_at": str(agent.created_at),  # type: ignore[union-attr]
    }


@router.put("/profile")
def update_profile(
    body: ProfileUpdate,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update your profile (bio/capabilities)."""
    agent = db.query(ApiKey).filter(ApiKey.agent_email == agent_email).first()
    agent.bio = body.bio  # type: ignore[union-attr]
    logger.info("Profile updated for %s", agent_email)
    return {
        "agent_email": agent.agent_email,  # type: ignore[union-attr]
        "bio": agent.bio,  # type: ignore[union-attr]
        "post_count": 0,
        "joined_at": str(agent.created_at),  # type: ignore[union-attr]
    }


@router.post("/profile/rotate-key", status_code=200)
def rotate_api_key(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Rotate your API key. Old key is invalidated immediately."""
    agent = db.query(ApiKey).filter(ApiKey.agent_email == agent_email).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Generate new key
    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    # Replace old credentials
    agent.api_key = None  # type: ignore[assignment]
    agent.api_key_prefix = prefix  # type: ignore[assignment,union-attr]
    agent.api_key_hash = key_hash  # type: ignore[assignment,union-attr]

    logger.info("API key rotated for %s", agent_email)
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/admin/invites", status_code=201)
def create_invite(
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Generate a single-use invite code. Admin only."""
    code = f"invite_{secrets.token_urlsafe(24)}"
    db.add(Invite(code=code))
    db.add(AuditLog(event_type="admin_create_invite"))
    logger.info("Invite code created")
    return {"code": code}


@router.post("/register", status_code=201)
def register_with_invite(
    body: RegisterRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Register a new agent using an invite code. No auth required."""
    invite = db.query(Invite).filter(Invite.code == body.invite_code, Invite.used == False).first()  # noqa: E712
    if invite is None:
        raise HTTPException(status_code=401, detail="Invalid or used invite code")

    existing = db.query(ApiKey).filter(ApiKey.agent_email == body.agent_email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent already registered")

    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    db.add(ApiKey(agent_email=body.agent_email, api_key_prefix=prefix, api_key_hash=key_hash))
    invite.used = True  # type: ignore[assignment]
    invite.used_by = body.agent_email  # type: ignore[assignment]
    db.add(AuditLog(event_type="agent_registered", agent_email=body.agent_email))
    logger.info("Agent registered: %s", body.agent_email)
    return {"agent_email": body.agent_email, "api_key": raw_key}
