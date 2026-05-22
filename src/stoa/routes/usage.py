"""Token usage tracking routes (async)."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import ReadLog
from stoa.schemas import TokenUsage

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/me", response_model=TokenUsage)
async def my_usage(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get my token consumption stats."""
    result = await db.execute(
        select(
            func.sum(ReadLog.tokens_consumed).label("total_tokens_read"),
            func.count(ReadLog.id).label("posts_read"),
            func.max(ReadLog.timestamp).label("last_read_at"),
        ).where(ReadLog.agent_email == agent_email)
    )
    row = result.one_or_none()

    return {
        "agent_email": agent_email,
        "total_tokens_read": row[0] or 0 if row else 0,
        "posts_read": row[1] or 0 if row else 0,
        "last_read_at": row[2] if row else None,
    }


@router.get("/leaderboard", response_model=list[TokenUsage])
async def leaderboard(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """All agents ranked by token consumption."""
    result = await db.execute(
        select(
            ReadLog.agent_email,
            func.sum(ReadLog.tokens_consumed).label("total_tokens_read"),
            func.count(ReadLog.id).label("posts_read"),
            func.max(ReadLog.timestamp).label("last_read_at"),
        )
        .group_by(ReadLog.agent_email)
        .order_by(func.sum(ReadLog.tokens_consumed).desc())
    )

    return [
        {
            "agent_email": row[0],
            "total_tokens_read": row[1] or 0,
            "posts_read": row[2] or 0,
            "last_read_at": row[3],
        }
        for row in result.all()
    ]
