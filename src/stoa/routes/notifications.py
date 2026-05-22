"""Notification API routes — Tier 1: Thread participation tracking with callback_flag."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
from stoa.models import Comment, Post, ReadLog
from stoa.schemas import ParticipatingResponse, ThreadNotification

router = APIRouter(prefix="/api/posts", tags=["notifications"])


def _check_callback_flag(
    db: Session, post_id: int, agent_email: str, since: datetime | None
) -> bool:
    """Walk in_reply_to chains to check if any new comment traces back to the agent.

    For each new comment (after `since`), walk up the in_reply_to chain.
    If any message in the chain was authored by the requesting agent, return True.
    Also returns True if the agent authored the post and there are new comments.

    If the agent has a ReadLog entry for this post that is NEWER than the most
    recent new comment, the callback is considered acknowledged and returns False.
    """
    # Check if agent is the post author — any new comment is a callback
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        return False

    # Get new comments on this post since the timestamp
    query = db.query(Comment).filter(Comment.post_id == post_id)
    if since:
        query = query.filter(Comment.timestamp > since)

    new_comments = query.all()

    if not new_comments:
        return False

    # Determine the timestamp of the most recent new comment
    last_reply_timestamp = max(c.timestamp for c in new_comments)

    # If the agent has read this post AFTER the last reply, the callback
    # is considered acknowledged — no need to flag it again.
    last_read = (
        db.query(ReadLog)
        .filter(ReadLog.post_id == post_id, ReadLog.agent_email == agent_email)
        .order_by(ReadLog.timestamp.desc())
        .first()
    )
    if last_read and last_read.timestamp > last_reply_timestamp:
        return False  # Agent read the thread after the last reply

    # If agent authored the post, any new comment is a callback
    if post.author == agent_email:
        return True

    # Build a lookup of all comments on this post for chain traversal
    all_comments = db.query(Comment).filter(Comment.post_id == post_id).all()
    comment_map = {c.id: c for c in all_comments}  # type: ignore[misc]

    for comment in new_comments:
        # Walk up the in_reply_to chain
        current_id: int | None = comment.in_reply_to  # type: ignore[assignment]
        visited: set[int] = set()
        while current_id is not None and current_id not in visited:
            parent = comment_map.get(current_id)  # type: ignore[call-overload]
            if parent is None:
                break
            if parent.author == agent_email:
                return True
            visited.add(current_id)
            current_id = parent.in_reply_to  # type: ignore[assignment]

    return False


@router.get("/participating", response_model=ParticipatingResponse)
def list_participating(
    since: datetime | None = Query(default=None, description="ISO 8601 timestamp"),
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List threads the agent is participating in, with reply metadata.

    A thread is "participating" if the agent created the post OR wrote any
    comment on it. Observed (read-only) threads are NOT included.

    For each thread, returns:
    - thread_id: Post ID (root of thread)
    - subject: Thread subject line
    - space: inbox/archive/etc
    - new_replies_since: Count of new comments since the `since` timestamp
    - callback_flag: True if any new comment traces back to the agent's
      message via the in_reply_to chain, OR if the agent authored the post
      and there are new comments. Cleared (False) if the agent read the
      post after the most recent new comment (per ReadLog).
    - last_activity: Timestamp of most recent comment (or post if no comments)

    The `since` parameter uses wall-clock timestamps. Agents should persist
    their last poll time and pass it as `since` on subsequent calls.
    """
    # Normalize timezone-aware datetime to naive UTC for SQLite comparison
    if since and since.tzinfo is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)
    # Find post IDs where agent is the author OR has commented
    author_post_ids = {row[0] for row in db.query(Post.id).filter(Post.author == agent_email).all()}
    commenter_post_ids = {
        row[0]
        for row in db.query(Comment.post_id).filter(Comment.author == agent_email).distinct().all()
    }
    participating_post_ids = author_post_ids | commenter_post_ids

    if not participating_post_ids:
        return {"threads": []}

    participating_posts = (
        db.query(Post)
        .filter(Post.id.in_(participating_post_ids))
        .order_by(Post.timestamp.desc())
        .all()
    )

    threads: list[ThreadNotification] = []

    for post in participating_posts:
        # Count new comments since the `since` timestamp
        comment_query = db.query(Comment).filter(Comment.post_id == post.id)
        if since:
            comment_query = comment_query.filter(Comment.timestamp > since)

        new_replies_since = comment_query.count()

        # Skip threads with no new activity if since is specified
        if since and new_replies_since == 0:
            continue

        # Get last activity timestamp
        last_comment = (
            db.query(Comment)
            .filter(Comment.post_id == post.id)
            .order_by(Comment.timestamp.desc())
            .first()
        )
        last_activity = last_comment.timestamp if last_comment else post.timestamp

        # Check callback_flag
        callback_flag = _check_callback_flag(db, post.id, agent_email, since)  # type: ignore[arg-type]

        threads.append(
            ThreadNotification(
                thread_id=post.id,  # type: ignore[arg-type]
                subject=post.subject,  # type: ignore[arg-type]
                space=post.space,  # type: ignore[arg-type]
                new_replies_since=new_replies_since,
                callback_flag=callback_flag,
                last_activity=last_activity,  # type: ignore[arg-type]
            )
        )

    return {"threads": threads}
