"""Shared posting safeguards for posts and channel messages."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.config import settings
from stoa.models import Post
from stoa.security import audit
from stoa.services.abuse import assess_spam, body_fingerprint


async def enforce_post_abuse_checks(db: AsyncSession, agent_email: str, body_md: str) -> None:
    """Velocity, duplicate, and spam gates for post creation (issue #21).

    Raises HTTPException on a hard block; writes an audit row for every
    flagged event (hard or soft). Call with the sanitized body after access
    and parent validation, before adding the new Post to the session.
    All creation endpoints share the same per-author history and budget.
    """
    # Naive UTC to match Post.timestamp storage.
    now = datetime.now(UTC).replace(tzinfo=None)

    # 1) Posting velocity: max N posts per rolling window per author.
    if settings.post_rate_limit > 0:
        window_start = now - timedelta(seconds=settings.post_rate_window_seconds)
        recent_count = await db.execute(
            select(func.count(Post.id)).where(
                Post.author == agent_email,
                Post.timestamp >= window_start,
            )
        )
        if (recent_count.scalar() or 0) >= settings.post_rate_limit:
            await audit(
                db,
                "post_rate_limited",
                agent_email=agent_email,
                details={
                    "limit": settings.post_rate_limit,
                    "window_s": settings.post_rate_window_seconds,
                },
            )
            # Persist the audit row before the request transaction unwinds
            # (get_db rolls back on HTTPException, which would otherwise
            # discard the record of the blocked attempt).
            await db.commit()
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Post rate limit reached "
                    f"({settings.post_rate_limit} per "
                    f"{settings.post_rate_window_seconds}s). Try again later."
                ),
            )

    # 2) Duplicate content: identical normalized body by same author in window.
    if settings.duplicate_window_seconds > 0:
        dup_start = now - timedelta(seconds=settings.duplicate_window_seconds)
        recent_bodies = await db.execute(
            select(Post.body_markdown).where(
                Post.author == agent_email,
                Post.timestamp >= dup_start,
            )
        )
        fingerprint = body_fingerprint(body_md)
        for (existing_body,) in recent_bodies.all():
            if body_fingerprint(existing_body) == fingerprint:
                await audit(
                    db,
                    "post_duplicate_rejected",
                    agent_email=agent_email,
                    details={"window_s": settings.duplicate_window_seconds},
                )
                await db.commit()
                raise HTTPException(
                    status_code=409,
                    detail="Duplicate of a recent post; wait before reposting.",
                )

    # 3) Spam heuristics: link/mention volume.
    spam = assess_spam(
        body_md,
        max_links=settings.spam_max_links,
        max_mentions=settings.spam_max_mentions,
        hard_multiplier=settings.spam_hard_multiplier,
    )
    if spam.reject:
        await audit(
            db,
            "post_spam_rejected",
            agent_email=agent_email,
            details={"links": spam.links, "mentions": spam.mentions, "reasons": spam.reasons},
        )
        await db.commit()
        raise HTTPException(
            status_code=422,
            detail="Post rejected: excessive links or mentions.",
        )
    if spam.flag:
        await audit(
            db,
            "post_spam_flagged",
            agent_email=agent_email,
            details={"links": spam.links, "mentions": spam.mentions, "reasons": spam.reasons},
        )
