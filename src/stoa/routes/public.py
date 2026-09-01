"""Public unauthenticated read routes for pinned posts in public channels.

Platform policy ("a pin escalates visibility within its channel's audience,
never beyond it"): pinned posts in public-visibility groups are readable
without an API key, read-only, and billed to no one — no ReadLog rows are
written for reads through this surface. Pinned posts in discoverable or
private groups stay inside the group boundary; the public endpoints do not
confirm their existence (404, never 403).

Identity policy: author emails are masked on this surface (local part
only — "alice@…"). Content visibility and identity visibility are
different classes: members who post or comment in a public channel never
opted into their addresses being scrapable, and a later pin must not
silently publish them. The authenticated surface is unchanged.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.models import Channel, Comment, Group, GroupVisibility, Post
from stoa.schemas import (
    PaginatedPublicPosts,
    PublicCommentOut,
    PublicPinnedSummary,
    PublicPostDetail,
)

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

    # Comment counts for the page in one grouped round trip (bounded by
    # limit<=100, but no reason to make it limit round trips).
    comment_counts: dict[int, int] = {}
    if posts:
        count_result = await db.execute(
            select(Comment.post_id, func.count(Comment.id))
            .where(Comment.post_id.in_([post.id for post in posts]))
            .group_by(Comment.post_id)
        )
        comment_counts = {post_id: count for post_id, count in count_result.all()}

    summaries = []
    for post in posts:
        channel_name, group_name = channels.get(post.channel_id or -1, ("", ""))
        # The author field is masked structurally by PublicPinnedSummary's
        # validator — the route cannot forget to do it.
        summaries.append(
            PublicPinnedSummary.model_validate(post, from_attributes=True).model_copy(
                update={
                    "comment_count": int(comment_counts.get(post.id, 0)),
                    "channel_name": channel_name,
                    "group_name": group_name,
                }
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/posts/{post_id}", response_model=PublicPostDetail)
async def get_public_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> PublicPostDetail:
    """Full detail for a pinned post in a public channel — no API key, no read billing.

    Author identities are masked (local part only) on this surface —
    members who commented on a pinned post never opted into their
    addresses being published; the authenticated surface is unchanged.

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
    # No token_cost on the public surface (and no count_tokens work):
    # reads here are billed to no one, so billing metadata would be a lie.
    comments = [
        PublicCommentOut.model_validate(c, from_attributes=True)
        for c in comment_result.scalars().all()
    ]

    # Constructed attribute-by-attribute (never model_validate on the
    # Post ORM object): a schema-level `comments` field would reach the
    # lazy `Post.comments` relationship, which cannot load in async
    # context — and the route wants timestamp order, not relationship
    # order, anyway.
    #
    # Deliberately no ReadLog: reads through the public surface are billed
    # to no one (platform policy).
    return PublicPostDetail(
        id=post.id,
        subject=post.subject,
        tldr=post.tldr,
        author=post.author,  # masked by the schema validator
        body_markdown=post.body_markdown,
        status=post.status,
        pinned=post.pinned,
        pinned_at=post.pinned_at,
        timestamp=post.timestamp,
        parent_post_id=post.parent_post_id,
        comments=comments,
    )
