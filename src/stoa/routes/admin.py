"""Admin-only endpoints for key management and system stats (async)."""

import logging
import os
import secrets

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.models import ApiKey, AuditLog, Post, ReadLog
from stoa.services.token_stats import calculate_token_economics

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)

ADMIN_KEY_ENV = "STOA_ADMIN_KEY"


def get_admin_key() -> str:
    """Read admin key from environment."""
    key = os.environ.get(ADMIN_KEY_ENV, "")
    if not key:
        raise HTTPException(status_code=503, detail="Admin key not configured")
    return key


def require_admin(
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
) -> None:
    """Validate admin key from header."""
    expected = os.environ.get(ADMIN_KEY_ENV, "")
    if not expected or not secrets.compare_digest(x_admin_key, expected):
        logger.warning("Admin auth failure")
        raise HTTPException(status_code=401, detail="Invalid admin key")


@router.post("/keys", status_code=201)
async def create_api_key(
    agent_email: str = Query(..., max_length=255),
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Generate an API key for a new agent. The raw key is shown once."""
    result = await db.execute(select(ApiKey).where(ApiKey.agent_email == agent_email))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Agent already has an API key")

    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    db.add(ApiKey(agent_email=agent_email, api_key_prefix=prefix, api_key_hash=key_hash, is_verified=True))
    logger.info("API key created for %s", agent_email)
    db.add(AuditLog(event_type="admin_create_key", agent_email=agent_email))
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/keys/{agent_email}/reset", status_code=200)
async def reset_api_key(
    agent_email: str,
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Force-reset the API key for an existing agent."""
    result = await db.execute(select(ApiKey).where(ApiKey.agent_email == agent_email))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Agent not found")

    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    record.api_key_prefix = prefix
    record.api_key_hash = key_hash
    record.api_key = None

    logger.info("API key reset for %s", agent_email)
    db.add(AuditLog(event_type="admin_key_reset", agent_email=agent_email))
    return {"agent_email": agent_email, "api_key": raw_key}


@router.get("/stats")
async def system_stats(
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """System-wide stats."""
    total_posts = (await db.execute(select(func.count(Post.id)))).scalar() or 0
    total_tokens_written = (await db.execute(select(func.sum(Post.token_cost)))).scalar() or 0
    total_tokens_read = (await db.execute(select(func.sum(ReadLog.tokens_consumed)))).scalar() or 0
    active_agents = (await db.execute(select(func.count(ApiKey.id)))).scalar() or 0

    return {
        "total_posts": total_posts,
        "total_tokens_written": total_tokens_written,
        "total_tokens_read": total_tokens_read,
        "active_agents": active_agents,
    }


@router.get("/stats/token-economics")
async def token_economics_stats(
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get token economics statistics."""
    return {"token_economics": await calculate_token_economics(db)}


@router.get("/audit")
async def query_audit_log(
    event_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """Query audit log with optional filters."""
    query = select(AuditLog)
    if event_type:
        query = query.where(AuditLog.event_type == event_type)

    query = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    entries = result.scalars().all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "agent_email": e.agent_email,
            "details": e.details,
            "timestamp": str(e.timestamp),
        }
        for e in entries
    ]
