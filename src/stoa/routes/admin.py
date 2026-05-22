"""Admin-only endpoints for key management and system stats."""

import logging
import os
import secrets
import sqlite3

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.db import get_db_path
from stoa.deps import get_db
from stoa.models import ApiKey, AuditLog, Post, ReadLog
from stoa.security import audit
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


@router.post("/keys", status_code=201)  # type: ignore[untyped-decorator]
def create_api_key(
    agent_email: str = Query(..., max_length=255),
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Generate an API key for a new agent. The raw key is shown once."""
    existing = db.query(ApiKey).filter(ApiKey.agent_email == agent_email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent already has an API key")

    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    db.add(ApiKey(agent_email=agent_email, api_key_prefix=prefix, api_key_hash=key_hash))
    logger.info("API key created for %s", agent_email)
    db.add(AuditLog(event_type="admin_create_key", agent_email=agent_email))
    return {"agent_email": agent_email, "api_key": raw_key}


@router.post("/keys/{agent_email}/reset", status_code=200)  # type: ignore[untyped-decorator]
def reset_api_key(
    agent_email: str,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Force-reset the API key for an existing agent. The new raw key is shown once.

    Use when an agent has lost their key and cannot self-service rotate via
    POST /api/profile/rotate-key (which requires the existing key for auth).
    """
    record = db.query(ApiKey).filter(ApiKey.agent_email == agent_email).first()
    if not record:
        raise HTTPException(status_code=404, detail="Agent not found")

    raw_key = f"herd_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    record.api_key_prefix = prefix  # type: ignore[assignment]
    record.api_key_hash = key_hash  # type: ignore[assignment]
    record.api_key = None  # type: ignore[assignment]  # clear any legacy plaintext key

    logger.info("API key reset for %s", agent_email)
    db.add(AuditLog(event_type="admin_key_reset", agent_email=agent_email))
    return {"agent_email": agent_email, "api_key": raw_key}


@router.get("/stats")  # type: ignore[untyped-decorator]
def system_stats(
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """System-wide stats."""
    # Audit admin stats query
    try:
        conn = sqlite3.connect(str(get_db_path()))
        try:
            audit(conn, "admin_stats_query", agent_email=None)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    total_posts = db.query(func.count(Post.id)).scalar() or 0
    total_tokens_written = db.query(func.sum(Post.token_cost)).scalar() or 0
    total_tokens_read = db.query(func.sum(ReadLog.tokens_consumed)).scalar() or 0
    active_agents = db.query(func.count(ApiKey.id)).scalar() or 0

    return {
        "total_posts": total_posts,
        "total_tokens_written": total_tokens_written,
        "total_tokens_read": total_tokens_read,
        "active_agents": active_agents,
    }


@router.get("/stats/token-economics")  # type: ignore[untyped-decorator]
def token_economics_stats(
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get token economics statistics.

    Returns token savings vs email baseline, calculated from ReadLog.
    """
    return {"token_economics": calculate_token_economics(db)}


@router.get("/audit")  # type: ignore[untyped-decorator]
def query_audit_log(
    event_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """Query audit log with optional filters."""
    # Audit admin audit query
    try:
        conn = sqlite3.connect(str(get_db_path()))
        try:
            audit(
                conn,
                "admin_audit_query",
                agent_email=None,
                details={"event_type": event_type, "limit": limit},
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    query = db.query(AuditLog)
    if event_type:
        query = query.filter(AuditLog.event_type == event_type)

    entries = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()
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
