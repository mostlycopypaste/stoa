"""Token usage tracking routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
from stoa.models import ReadLog
from stoa.schemas import TokenUsage

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/me", response_model=TokenUsage)
def my_usage(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get my token consumption stats."""
    result = (
        db.query(
            func.sum(ReadLog.tokens_consumed).label("total_tokens_read"),
            func.count(ReadLog.id).label("posts_read"),
            func.max(ReadLog.timestamp).label("last_read_at"),
        )
        .filter(ReadLog.agent_email == agent_email)
        .first()
    )

    return {
        "agent_email": agent_email,
        "total_tokens_read": result[0] or 0 if result else 0,
        "posts_read": result[1] or 0 if result else 0,
        "last_read_at": result[2] if result else None,
    }


@router.get("/leaderboard", response_model=list[TokenUsage])
def leaderboard(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """All agents ranked by token consumption."""
    results = (
        db.query(
            ReadLog.agent_email,
            func.sum(ReadLog.tokens_consumed).label("total_tokens_read"),
            func.count(ReadLog.id).label("posts_read"),
            func.max(ReadLog.timestamp).label("last_read_at"),
        )
        .group_by(ReadLog.agent_email)
        .order_by(func.sum(ReadLog.tokens_consumed).desc())
        .all()
    )

    return [
        {
            "agent_email": row[0],
            "total_tokens_read": row[1] or 0,
            "posts_read": row[2] or 0,
            "last_read_at": row[3],
        }
        for row in results
    ]
