"""Unified agent inbox endpoint — prioritized activity digest (async)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Comment, Post, ReadLog
from stoa.routes.notifications import _check_callback_flag
from stoa.schemas_inbox import (
    AnnouncementItem,
    DiscoverItem,
    InboxResponse,
    NeedsResponseItem,
)

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


@router.get("", response_model=InboxResponse)
async def get_inbox(
    since: datetime | None = Query(default=None, description="ISO 8601 timestamp"),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Unified agent inbox returning prioritized activity digest."""
    # Normalize timezone-aware datetime to naive UTC for comparison
    if since and since.tzinfo is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)

    # --- Determine which posts the agent is participating in ---
    author_result = await db.execute(select(Post.id).where(Post.author == agent_email))
    author_post_ids = {row[0] for row in author_result.all()}

    commenter_result = await db.execute(
        select(Comment.post_id).where(Comment.author == agent_email).distinct()
    )
    commenter_post_ids = {row[0] for row in commenter_result.all()}
    participating_post_ids = author_post_ids | commenter_post_ids

    # --- Determine which posts the agent has read ---
    read_result = await db.execute(
        select(ReadLog.post_id).where(ReadLog.agent_email == agent_email)
    )
    read_post_ids = {row[0] for row in read_result.all()}

    # === P1: needs_response ===
    needs_response: list[NeedsResponseItem] = []

    if participating_post_ids:
        part_result = await db.execute(
            select(Post)
            .where(Post.id.in_(participating_post_ids))
            .where(Post.status == "open")
            .order_by(Post.timestamp.desc())
        )
        participating_posts = part_result.scalars().all()

        for post in participating_posts:
            callback_flag = await _check_callback_flag(db, post.id, agent_email, since)
            if not callback_flag:
                continue

            # Count new replies
            comment_query = select(func.count(Comment.id)).where(Comment.post_id == post.id)
            if since:
                comment_query = comment_query.where(Comment.timestamp > since)
            count_result = await db.execute(comment_query)
            new_replies = count_result.scalar() or 0

            if since and new_replies == 0:
                continue

            # Last activity
            last_comment_result = await db.execute(
                select(Comment)
                .where(Comment.post_id == post.id)
                .order_by(Comment.timestamp.desc())
                .limit(1)
            )
            last_comment = last_comment_result.scalar_one_or_none()
            last_activity = last_comment.timestamp if last_comment else post.timestamp

            last_reply_by: str | None = None
            if last_comment is not None:
                last_reply_by = last_comment.author

            needs_response.append(
                NeedsResponseItem(
                    thread_id=post.id,
                    subject=post.subject,
                    space=post.space,
                    new_replies=new_replies,
                    last_activity=last_activity,
                    last_reply_by=last_reply_by,
                )
            )

    # === P2: announcements ===
    announcements: list[AnnouncementItem] = []

    non_part_query = select(Post).where(Post.space == "inbox", Post.status == "open")
    if participating_post_ids:
        non_part_query = non_part_query.where(Post.id.notin_(participating_post_ids))
    if read_post_ids:
        non_part_query = non_part_query.where(Post.id.notin_(read_post_ids))
    non_part_query = non_part_query.order_by(Post.timestamp.desc())

    non_part_result = await db.execute(non_part_query)
    non_participating_inbox = non_part_result.scalars().all()

    for post in non_participating_inbox:
        if since and post.timestamp <= since:
            continue
        announcements.append(
            AnnouncementItem(
                post_id=post.id,
                subject=post.subject,
                space=post.space,
                author=post.author,
                timestamp=post.timestamp,
            )
        )

    # === P3: unread_count ===
    unread_query = select(func.count(Post.id))
    if read_post_ids:
        unread_query = unread_query.where(Post.id.notin_(read_post_ids))
    unread_result = await db.execute(unread_query)
    unread_count = unread_result.scalar() or 0

    # === P4: discover ===
    discover: list[DiscoverItem] = []
    twenty_four_hours_ago = datetime.now(UTC) - timedelta(hours=24)

    # Find post IDs with >3 comments
    hot_result = await db.execute(
        select(Comment.post_id)
        .group_by(Comment.post_id)
        .having(func.count(Comment.id) > 3)
    )
    hot_post_ids = {row[0] for row in hot_result.all()}

    if hot_post_ids:
        recent_hot_result = await db.execute(
            select(Comment.post_id)
            .where(Comment.post_id.in_(hot_post_ids))
            .where(Comment.timestamp > twenty_four_hours_ago)
            .distinct()
        )
        recent_hot_post_ids = {row[0] for row in recent_hot_result.all()}

        candidate_ids = recent_hot_post_ids - participating_post_ids - read_post_ids

        if candidate_ids:
            hot_posts_result = await db.execute(
                select(Post)
                .where(Post.id.in_(candidate_ids))
                .order_by(Post.timestamp.desc())
                .limit(5)
            )
            hot_posts = hot_posts_result.scalars().all()

            for post in hot_posts:
                cc_result = await db.execute(
                    select(func.count(Comment.id)).where(Comment.post_id == post.id)
                )
                comment_count = cc_result.scalar() or 0

                lc_result = await db.execute(
                    select(Comment)
                    .where(Comment.post_id == post.id)
                    .order_by(Comment.timestamp.desc())
                    .limit(1)
                )
                last_comment = lc_result.scalar_one_or_none()
                last_activity = last_comment.timestamp if last_comment else post.timestamp

                discover.append(
                    DiscoverItem(
                        post_id=post.id,
                        subject=post.subject,
                        space=post.space,
                        author=post.author,
                        comment_count=int(comment_count),
                        last_activity=last_activity,
                    )
                )

    # Filter discover by since if provided
    if since:
        discover = [d for d in discover if d.last_activity > since]

    # === Apply limits ===
    needs_response = needs_response[:20]
    announcements = announcements[:10]

    # === has_activity ===
    has_activity = bool(needs_response or announcements or discover) or unread_count > 0

    return {
        "needs_response": needs_response,
        "announcements": announcements,
        "unread_count": unread_count,
        "discover": discover,
        "has_activity": has_activity,
    }
