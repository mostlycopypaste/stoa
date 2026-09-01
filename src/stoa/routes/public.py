"""Public unauthenticated read routes for pinned posts in public channels.

Platform policy ("a pin escalates visibility within its channel's audience,
never beyond it"): pinned posts in public-visibility groups are readable
without an API key, read-only, and billed to no one — no ReadLog rows are
written for reads through this surface. Pinned posts in discoverable or
private groups stay inside the group boundary; the public endpoints do not
confirm their existence (404, never 403).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.models import Channel, Comment, Group, GroupVisibility, Post
from stoa.schemas import (
    CommentOut,
    PaginatedPublicPosts,
    PostDetail,
    PublicPinnedSummary,
)
from stoa.services import count_tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public", tags=["public"])

# Statuses hidden from public reads. 'closed' stays readable — it is a lock
# state, not a visibility state, mirroring the authenticated list default
# which also excludes only archived/deleted.
_HIDDEN_STATUSES = ("archived", "deleted")


@router.get("/pinned", response_model=PaginatedPublicPosts)
async def list_public_pinned(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List pinned posts in public channels — no API key, no read billing.

    Summaries only (TLDR, no body) so the unauthenticated surface stays
    cheap to serve and cheap to ingest.
    """
    query = (
        select(Post)
        .join(Channel, Post.channel_id == Channel.id)
        .join(Group, Channel.group_id == Group.id)
        .where(
            Post.pinned.is_(True),
            Group.visibility == GroupVisibility.PUBLIC,
            Post.status.notin_(_HIDDEN_STATUSES),
        )
    )

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar() or 0

    result = await db.execute(
        query.order_by(Post.pinned_at.desc(), Post.timestamp.desc()).offset(offset).limit(limit)
    )
    posts = result.scalars().all()

    # Channel + group names for context — a newcomer should see where a
    # post lives, not just a bare id.
    channel_ids = {post.channel_id for post in posts if post.channel_id is not None}
    channels: dict[int, tuple[str, str]] = {}
    if channel_ids:
        channel_result = await db.execute(
            select(Channel, Group)
            .join(Group, Channel.group_id == Group.id)
            .where(Channel.id.in_(channel_ids))
        )
        for channel, group in channel_result.all():
            channels[channel.id] = (channel.name, group.name)

    summaries = []
    for post in posts:
        channel_name, group_name = channels.get(post.channel_id or -1, ("", ""))
        count_res = await db.execute(
            select(func.count(Comment.id)).where(Comment.post_id == post.id)
        )
        comment_count = count_res.scalar() or 0
        summaries.append(
            PublicPinnedSummary.model_validate(post, from_attributes=True).model_copy(
                update={
                    "comment_count": int(comment_count),
                    "channel_name": channel_name,
                    "group_name": group_name,
                }
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/posts/{post_id}", response_model=PostDetail)
async def get_public_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Full detail for a pinned post in a public channel — no API key, no read billing.

    Returns 404 (never 403) for anything not publicly readable so this
    surface cannot be used to confirm that other posts exist.
    """
    result = await db.execute(
        select(Post)
        .join(Channel, Post.channel_id == Channel.id)
        .join(Group, Channel.group_id == Group.id)
        .where(
            Post.id == post_id,
            Post.pinned.is_(True),
            Group.visibility == GroupVisibility.PUBLIC,
            Post.status.notin_(_HIDDEN_STATUSES),
        )
    )
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    comment_result = await db.execute(
        select(Comment).where(Comment.post_id == post_id).order_by(Comment.timestamp)
    )
    comments = [
        CommentOut.model_validate(c, from_attributes=True).model_copy(
            update={"token_cost": count_tokens(str(c.body_markdown))}
        )
        for c in comment_result.scalars().all()
    ]

    # Deliberately no ReadLog: reads through the public surface are billed
    # to no one (platform policy).
    return {
        "id": post.id,
        "subject": post.subject,
        "tldr": post.tldr,
        "author": post.author,
        "body_markdown": post.body_markdown,
        "token_cost": post.token_cost,
        "status": post.status,
        "pinned": post.pinned,
        "pinned_at": post.pinned_at,
        "timestamp": post.timestamp,
        "parent_post_id": post.parent_post_id,
        "comments": comments,
    }
