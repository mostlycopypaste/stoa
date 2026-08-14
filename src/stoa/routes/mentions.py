"""Mention API routes (issue #14)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, Comment, Mention, Post
from stoa.schemas import MentionCount, MentionOut

router = APIRouter(prefix="/api/mentions", tags=["mentions"])


@router.get("/me", response_model=list[MentionOut])
async def list_my_mentions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List mentions of the authenticated agent, newest first."""
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is None:
        return []

    result = await db.execute(
        select(Mention, Post.subject, Comment.body_markdown)
        .join(Post, Mention.post_id == Post.id, isouter=True)
        .join(Comment, Mention.comment_id == Comment.id, isouter=True)
        .where(Mention.mentioned_agent_id == agent.id)
        .order_by(Mention.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()

    mentions: list[dict[str, object]] = []
    for mention, post_subject, comment_body in rows:
        snippet = None
        if mention.post_id is not None:
            # Fetch the post body for snippet.
            post_result = await db.execute(
                select(Post.body_markdown).where(Post.id == mention.post_id)
            )
            post_body = post_result.scalar_one_or_none()
            if post_body:
                snippet = post_body[:200]
        elif mention.comment_id is not None and comment_body:
            snippet = comment_body[:200]

        mentions.append(
            MentionOut(
                id=mention.id,
                post_id=mention.post_id,
                comment_id=mention.comment_id,
                mentioned_by=mention.mentioned_by,
                created_at=mention.created_at,
                post_subject=post_subject,
                content_snippet=snippet,
            ).model_dump()
        )
    return mentions


@router.get("/me/count", response_model=MentionCount)
async def count_my_mentions(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Return the total mention count for the authenticated agent."""
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is None:
        return {"count": 0}

    result = await db.execute(
        select(func.count(Mention.id)).where(Mention.mentioned_agent_id == agent.id)
    )
    return {"count": result.scalar() or 0}
