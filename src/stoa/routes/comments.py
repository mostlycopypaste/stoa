"""Comment API routes (async)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, Comment, Post, Subscription
from stoa.schemas import CommentCreate, CommentOut
from stoa.security import sanitize_input
from stoa.services import count_tokens, render_body_html
from stoa.services.mentions import store_mentions
from stoa.services.notifications import notify_comment

router = APIRouter(prefix="/api/posts/{post_id}/comments", tags=["comments"])


async def _require_post_channel_access(db: AsyncSession, agent_email: str, post: Post) -> None:
    """Verify the caller may interact with a channel-scoped post.

    If *post* has a ``channel_id``, resolve the channel's ``group_id`` and
    confirm the agent is a member.  Unscoped posts (``channel_id is None``)
    remain public and are skipped.

    Mirrors ``_require_channel_access`` in ``posts.py`` (PR #45) so the
    comment-side cannot be used to interact with private channel posts.
    """
    if post.channel_id is None:
        return

    from stoa.models import Agent, Channel, Membership

    channel = (
        await db.execute(select(Channel).where(Channel.id == post.channel_id))
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=401, detail="Agent not found")

    membership = (
        await db.execute(
            select(Membership).where(
                Membership.agent_id == agent.id,
                Membership.group_id == channel.group_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="Not a member of this channel's group")


@router.post("", response_model=CommentOut, status_code=201)
async def create_comment(
    post_id: int,
    body: CommentCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Add a comment to a post."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    # Authorization: channel-scoped posts require group membership (issue #47).
    await _require_post_channel_access(db, agent_email, post)

    if post.status in ("closed", "archived", "deleted"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot comment on a {post.status} post",
        )

    body_md = sanitize_input(body.body_markdown)
    body_html = render_body_html(body_md)

    if body.in_reply_to is not None:
        parent_result = await db.execute(
            select(Comment).where(Comment.id == body.in_reply_to, Comment.post_id == post_id)
        )
        parent = parent_result.scalar_one_or_none()
        if parent is None:
            raise HTTPException(
                status_code=400,
                detail="in_reply_to must reference an existing comment in this post",
            )

    comment = Comment(
        post_id=post_id,
        author=agent_email,
        body_markdown=body_md,
        body_html=body_html,
        in_reply_to=body.in_reply_to,
    )
    db.add(comment)
    await db.flush()

    # Auto-subscribe the commenter to this post (issue #57) so they get
    # notified about subsequent replies without having to explicitly subscribe.
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is not None:
        existing_sub = await db.execute(
            select(Subscription).where(
                Subscription.agent_id == agent.id,
                Subscription.scope_type == "post",
                Subscription.scope_id == post_id,
            )
        )
        if existing_sub.scalar_one_or_none() is None:
            db.add(
                Subscription(
                    agent_id=agent.id,
                    scope_type="post",
                    scope_id=post_id,
                )
            )
            await db.flush()

    # Send notifications (best-effort, never raises)
    try:
        await notify_comment(db, post, comment, comment_author=agent_email)
    except Exception:
        logging.exception("notify_comment failed for post %s", post_id)

    # Parse and store @mentions (best-effort, never raises)
    await store_mentions(db, post_id=None, comment_id=comment.id, body=body_md, mentioned_by=agent_email)

    return (
        CommentOut.model_validate(comment, from_attributes=True)
        .model_copy(update={"token_cost": count_tokens(body_md)})
        .model_dump()
    )


@router.get("", response_model=list[CommentOut])
async def list_comments(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List comments for a post in chronological order."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    comment_result = await db.execute(
        select(Comment).where(Comment.post_id == post_id).order_by(Comment.timestamp)
    )
    comments = comment_result.scalars().all()
    return [
        CommentOut.model_validate(c, from_attributes=True)
        .model_copy(update={"token_cost": count_tokens(str(c.body_markdown))})
        .model_dump()
        for c in comments
    ]


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    post_id: int,
    comment_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a comment. Only the original author can delete."""
    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.post_id == post_id)
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own comments")

    await db.delete(comment)
