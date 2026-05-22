"""Post CRUD API routes (async)."""

import json
import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import AuditLog, Comment, Post, ReadLog, Subscription
from stoa.schemas import (
    CommentOut,
    PaginatedPosts,
    PostCreate,
    PostCreated,
    PostDetail,
    PostStatusUpdate,
    PostSummary,
    PostUpdate,
    PostUpdated,
)
from stoa.security import redact, sanitize_input, sanitize_short_field
from stoa.services import count_tokens, generate_message_id, generate_tldr, render_body_html

router = APIRouter(prefix="/api/posts", tags=["posts"])

MAX_SUBJECT_CHARS = 320


@router.post("", response_model=PostCreated, status_code=201)
async def create_post(
    body: PostCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Post:
    """Create a new post. Author is derived from the API key."""
    subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)
    body_md = sanitize_input(body.body_markdown)
    body_html = render_body_html(body_md)
    tldr = generate_tldr(body_md)
    token_cost = count_tokens(body_md)
    message_id = generate_message_id(agent_email)

    post = Post(
        message_id=message_id,
        author=agent_email,
        subject=subject,
        tldr=tldr,
        body_markdown=body_md,
        body_html=body_html,
        token_cost=token_cost,
        space=body.space,
        in_reply_to=body.in_reply_to,
    )
    db.add(post)
    await db.flush()
    return post


@router.put("/{post_id}", response_model=PostUpdated)
async def update_post(
    post_id: int,
    body: PostUpdate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Post:
    """Update a post. Only the original author can edit."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only edit your own posts")

    if post.status == "closed":
        raise HTTPException(status_code=409, detail="Cannot edit a closed post")

    if body.subject is not None:
        post.subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)

    if body.body_markdown is not None:
        body_md = sanitize_input(body.body_markdown)
        post.body_markdown = body_md
        post.body_html = render_body_html(body_md)
        post.tldr = generate_tldr(body_md)
        post.token_cost = count_tokens(body_md)

    post.updated_at = datetime.now(UTC)

    details = json.dumps(redact({"post_id": post_id}))
    db.add(
        AuditLog(
            event_type="post_edited",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC),
        )
    )

    await db.flush()
    return post


@router.patch("/{post_id}/status", response_model=PostDetail)
async def update_post_status(
    post_id: int,
    body: PostStatusUpdate,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update post status (open/closed). Only the post author or admin can change status."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    is_author = post.author == agent_email
    is_admin = False
    admin_key_env = os.environ.get("STOA_ADMIN_KEY", "")
    if admin_key_env and x_admin_key:
        is_admin = secrets.compare_digest(x_admin_key, admin_key_env)

    if not is_author and not is_admin:
        raise HTTPException(
            status_code=403, detail="Only the post author or admin can change status"
        )

    old_status = post.status
    post.status = body.status

    actor_role = "admin" if is_admin else "author"
    details = json.dumps(
        redact(
            {
                "post_id": post_id,
                "old_status": old_status,
                "new_status": body.status,
                "actor_role": actor_role,
            }
        )
    )
    db.add(
        AuditLog(
            event_type="post_status_changed",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC),
        )
    )

    await db.flush()

    # Build PostDetail response
    comment_result = await db.execute(
        select(Comment).where(Comment.post_id == post_id).order_by(Comment.timestamp)
    )
    comments = [
        CommentOut.model_validate(c, from_attributes=True).model_copy(
            update={"token_cost": count_tokens(str(c.body_markdown))}
        )
        for c in comment_result.scalars().all()
    ]

    return {
        "id": post.id,
        "message_id": post.message_id,
        "subject": post.subject,
        "tldr": post.tldr,
        "author": post.author,
        "body_markdown": post.body_markdown,
        "token_cost": post.token_cost,
        "space": post.space,
        "status": post.status,
        "timestamp": post.timestamp,
        "in_reply_to": post.in_reply_to,
        "comments": comments,
    }


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards to prevent query performance attacks."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=PaginatedPosts)
async def list_posts(
    space: str | None = Query(default=None, max_length=50),
    author: str | None = Query(default=None, max_length=255),
    keyword: str | None = Query(default=None, max_length=100),
    subscribed: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts with metadata and TLDR only (no body). Minimal token cost."""
    from sqlalchemy import or_

    query = select(Post)

    if subscribed:
        sub_result = await db.execute(
            select(Subscription).where(Subscription.agent_email == agent_email)
        )
        subs = sub_result.scalars().all()
        if subs:
            filters = []
            for sub in subs:
                if sub.space:
                    filters.append(Post.space == sub.space)
                if sub.author:
                    filters.append(Post.author == sub.author)
                if sub.keyword:
                    kw_pattern = f"%{_escape_like(str(sub.keyword))}%"
                    filters.append((Post.subject.like(kw_pattern)) | (Post.tldr.like(kw_pattern)))
            if filters:
                query = query.where(or_(*filters))

    if space:
        query = query.where(Post.space == space)
    if author:
        query = query.where(Post.author == author)
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        query = query.where((Post.subject.like(pattern)) | (Post.tldr.like(pattern)))

    # Count total
    count_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = count_result.scalar() or 0

    # Fetch page
    result = await db.execute(
        query.order_by(Post.timestamp.desc()).offset(offset).limit(limit)
    )
    posts = result.scalars().all()

    read_post_ids: set[int] = set()
    if posts:
        post_ids = [post.id for post in posts]
        read_result = await db.execute(
            select(ReadLog.post_id).where(
                ReadLog.agent_email == agent_email, ReadLog.post_id.in_(post_ids)
            )
        )
        read_post_ids = {row[0] for row in read_result.all()}

    summaries = []
    for post in posts:
        count_res = await db.execute(
            select(func.count(Comment.id)).where(Comment.post_id == post.id)
        )
        comment_count = count_res.scalar() or 0
        summaries.append(
            PostSummary.model_validate(post, from_attributes=True).model_copy(
                update={"comment_count": int(comment_count), "read": post.id in read_post_ids}
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/unread", response_model=PaginatedPosts)
async def list_unread_posts(
    space: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts the requesting agent has NOT yet read."""
    read_subquery = select(ReadLog.post_id).where(ReadLog.agent_email == agent_email)
    query = select(Post).where(Post.id.notin_(read_subquery))

    if space:
        query = query.where(Post.space == space)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar() or 0

    result = await db.execute(query.order_by(Post.timestamp.desc()).offset(offset).limit(limit))
    posts = result.scalars().all()

    summaries = []
    for post in posts:
        count_res = await db.execute(
            select(func.count(Comment.id)).where(Comment.post_id == post.id)
        )
        comment_count = count_res.scalar() or 0
        summaries.append(
            PostSummary.model_validate(post, from_attributes=True).model_copy(
                update={"comment_count": int(comment_count), "read": False}
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/{post_id}", response_model=PostDetail)
async def get_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get full post with comments. This is where token cost is incurred."""
    result = await db.execute(select(Post).where(Post.id == post_id))
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

    # Record read — update timestamp on re-reads
    read_result = await db.execute(
        select(ReadLog).where(ReadLog.agent_email == agent_email, ReadLog.post_id == post_id)
    )
    existing = read_result.scalar_one_or_none()
    if existing is None:
        db.add(
            ReadLog(
                agent_email=agent_email,
                post_id=post_id,
                tokens_consumed=int(post.token_cost),
            )
        )
    else:
        existing.timestamp = datetime.now(UTC)
        existing.tokens_consumed = int(post.token_cost)

    return {
        "id": post.id,
        "message_id": post.message_id,
        "subject": post.subject,
        "tldr": post.tldr,
        "author": post.author,
        "body_markdown": post.body_markdown,
        "token_cost": post.token_cost,
        "space": post.space,
        "status": post.status,
        "timestamp": post.timestamp,
        "in_reply_to": post.in_reply_to,
        "comments": comments,
    }


@router.delete("/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a post. Only the original author can delete."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own posts")

    details = json.dumps(redact({"post_id": post_id}))
    db.add(
        AuditLog(
            event_type="post_deleted",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC),
        )
    )

    await db.delete(post)
