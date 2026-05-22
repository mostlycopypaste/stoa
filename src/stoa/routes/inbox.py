"""Unified agent inbox endpoint — prioritized activity digest."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
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
def get_inbox(
    since: datetime | None = Query(default=None, description="ISO 8601 timestamp"),
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Unified agent inbox returning prioritized activity digest.

    Returns four tiers of information:
    - P1 (needs_response): Participating threads with callback_flag=true
    - P2 (announcements): Unread inbox posts the agent isn't participating in
    - P3 (unread_count): Total unread post count
    - P4 (discover): Hot threads (>3 comments, last 24h) the agent isn't in
    """
    # Normalize timezone-aware datetime to naive UTC for SQLite comparison
    if since and since.tzinfo is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)

    # --- Determine which posts the agent is participating in ---
    author_post_ids = {row[0] for row in db.query(Post.id).filter(Post.author == agent_email).all()}
    commenter_post_ids = {
        row[0]
        for row in db.query(Comment.post_id).filter(Comment.author == agent_email).distinct().all()
    }
    participating_post_ids = author_post_ids | commenter_post_ids

    # --- Determine which posts the agent has read ---
    read_post_ids = {
        row[0] for row in db.query(ReadLog.post_id).filter(ReadLog.agent_email == agent_email).all()
    }

    # === P1: needs_response ===
    # Participating threads with callback_flag=true (exclude closed posts)
    needs_response: list[NeedsResponseItem] = []

    if participating_post_ids:
        participating_posts = (
            db.query(Post)
            .filter(Post.id.in_(participating_post_ids))
            .filter(Post.status == "open")
            .order_by(Post.timestamp.desc())
            .all()
        )

        for post in participating_posts:
            callback_flag = _check_callback_flag(db, post.id, agent_email, since)  # type: ignore[arg-type]
            if not callback_flag:
                continue

            # Count new replies
            comment_query = db.query(Comment).filter(Comment.post_id == post.id)
            if since:
                comment_query = comment_query.filter(Comment.timestamp > since)
            new_replies = comment_query.count()

            # Skip threads with no new activity if since is specified
            if since and new_replies == 0:
                continue

            # Last activity
            last_comment = (
                db.query(Comment)
                .filter(Comment.post_id == post.id)
                .order_by(Comment.timestamp.desc())
                .first()
            )
            last_activity = last_comment.timestamp if last_comment else post.timestamp  # type: ignore[union-attr]

            # Last reply author
            last_reply_by: str | None = None
            if last_comment is not None:
                last_reply_by = last_comment.author  # type: ignore[union-attr, assignment]

            needs_response.append(
                NeedsResponseItem(
                    thread_id=post.id,  # type: ignore[arg-type]
                    subject=post.subject,  # type: ignore[arg-type]
                    space=post.space,  # type: ignore[arg-type]
                    new_replies=new_replies,
                    last_activity=last_activity,  # type: ignore[arg-type]
                    last_reply_by=last_reply_by,
                )
            )

    # === P2: announcements ===
    # Unread posts in "inbox" space that the agent is NOT participating in (exclude closed)
    announcements: list[AnnouncementItem] = []

    non_participating_query = db.query(Post).filter(Post.space == "inbox", Post.status == "open")
    if participating_post_ids:
        non_participating_query = non_participating_query.filter(
            Post.id.notin_(participating_post_ids)
        )
    if read_post_ids:
        non_participating_query = non_participating_query.filter(Post.id.notin_(read_post_ids))
    non_participating_inbox = non_participating_query.order_by(Post.timestamp.desc()).all()

    for post in non_participating_inbox:
        # If since is specified, only include posts created after that time
        if since and post.timestamp <= since:  # type: ignore[union-attr]
            continue

        announcements.append(
            AnnouncementItem(
                post_id=post.id,  # type: ignore[arg-type]
                subject=post.subject,  # type: ignore[arg-type]
                space=post.space,  # type: ignore[arg-type]
                author=post.author,  # type: ignore[arg-type]
                timestamp=post.timestamp,  # type: ignore[arg-type]
            )
        )

    # === P3: unread_count ===
    unread_count_query = db.query(Post)
    if read_post_ids:
        unread_count_query = unread_count_query.filter(Post.id.notin_(read_post_ids))
    unread_count = unread_count_query.count()

    # === P4: discover ===
    # Hot threads: >3 comments, last comment within 24h, agent not participating, agent hasn't read
    discover: list[DiscoverItem] = []

    twenty_four_hours_ago = datetime.now(UTC) - timedelta(hours=24)

    # Find post IDs with >3 comments
    hot_post_ids_query = (
        db.query(Comment.post_id).group_by(Comment.post_id).having(func.count(Comment.id) > 3).all()
    )
    hot_post_ids = {row[0] for row in hot_post_ids_query}

    if hot_post_ids:
        # Filter to those with last comment within 24h
        recent_hot_post_ids = {
            row[0]
            for row in db.query(Comment.post_id)
            .filter(Comment.post_id.in_(hot_post_ids))
            .filter(Comment.timestamp > twenty_four_hours_ago)
            .distinct()
            .all()
        }

        # Exclude posts the agent is participating in or has already read
        candidate_ids = recent_hot_post_ids - participating_post_ids - read_post_ids

        if candidate_ids:
            hot_posts = (
                db.query(Post)
                .filter(Post.id.in_(candidate_ids))
                .order_by(Post.timestamp.desc())
                .limit(5)
                .all()
            )

            for post in hot_posts:
                comment_count = (
                    db.query(func.count(Comment.id)).filter(Comment.post_id == post.id).scalar()
                    or 0
                )
                last_comment = (
                    db.query(Comment)
                    .filter(Comment.post_id == post.id)
                    .order_by(Comment.timestamp.desc())
                    .first()
                )
                last_activity = last_comment.timestamp if last_comment else post.timestamp  # type: ignore[union-attr]

                discover.append(
                    DiscoverItem(
                        post_id=post.id,  # type: ignore[arg-type]
                        subject=post.subject,  # type: ignore[arg-type]
                        space=post.space,  # type: ignore[arg-type]
                        author=post.author,  # type: ignore[arg-type]
                        comment_count=int(comment_count),
                        last_activity=last_activity,  # type: ignore[union-attr, arg-type]
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
