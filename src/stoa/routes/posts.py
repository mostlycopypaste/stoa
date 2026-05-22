"""Post CRUD API routes."""

import json
import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
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
def create_post(
    body: PostCreate,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
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
    db.flush()
    return post


@router.put("/{post_id}", response_model=PostUpdated)
def update_post(
    post_id: int,
    body: PostUpdate,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> Post:
    """Update a post. Only the original author can edit."""
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only edit your own posts")

    if post.status == "closed":
        raise HTTPException(status_code=409, detail="Cannot edit a closed post")

    if body.subject is not None:
        post.subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)  # type: ignore[assignment]

    if body.body_markdown is not None:
        body_md = sanitize_input(body.body_markdown)
        post.body_markdown = body_md  # type: ignore[assignment]
        post.body_html = render_body_html(body_md)  # type: ignore[assignment]
        post.tldr = generate_tldr(body_md)  # type: ignore[assignment]
        post.token_cost = count_tokens(body_md)  # type: ignore[assignment]

    post.updated_at = datetime.now(UTC)  # type: ignore[assignment]

    # Audit the edit using the same SQLAlchemy session for atomicity
    details = json.dumps(redact({"post_id": post_id}))
    db.add(
        AuditLog(
            event_type="post_edited",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC),
        )
    )

    db.flush()

    return post


@router.patch("/{post_id}/status", response_model=PostDetail)
def update_post_status(
    post_id: int,
    body: PostStatusUpdate,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update post status (open/closed). Only the post author or admin can change status."""
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    # Check authorization: author or admin
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
    post.status = body.status  # type: ignore[assignment]

    # Audit the status change
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

    db.flush()

    # Build PostDetail response
    comments = [
        CommentOut.model_validate(c, from_attributes=True).model_copy(
            update={"token_cost": count_tokens(str(c.body_markdown))}
        )
        for c in db.query(Comment)
        .filter(Comment.post_id == post_id)
        .order_by(Comment.timestamp)
        .all()
    ]

    detail = PostDetail.model_validate(post, from_attributes=True).model_copy(
        update={"comments": comments}
    )
    return detail.model_dump()


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards to prevent query performance attacks."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=PaginatedPosts)
def list_posts(
    space: str | None = Query(default=None, max_length=50),
    author: str | None = Query(default=None, max_length=255),
    keyword: str | None = Query(default=None, max_length=100),
    subscribed: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts with metadata and TLDR only (no body). Minimal token cost."""
    query = db.query(Post)

    if subscribed:
        subs = db.query(Subscription).filter(Subscription.agent_email == agent_email).all()
        if subs:
            from sqlalchemy import or_

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
                query = query.filter(or_(*filters))

    if space:
        query = query.filter(Post.space == space)
    if author:
        query = query.filter(Post.author == author)
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        query = query.filter((Post.subject.like(pattern)) | (Post.tldr.like(pattern)))

    total = query.count()
    posts = query.order_by(Post.timestamp.desc()).offset(offset).limit(limit).all()

    read_post_ids = set()
    if posts:
        post_ids = [post.id for post in posts]
        read_rows = (
            db.query(ReadLog.post_id)
            .filter(ReadLog.agent_email == agent_email, ReadLog.post_id.in_(post_ids))
            .all()
        )
        read_post_ids = {row[0] for row in read_rows}

    summaries = []
    for post in posts:
        comment_count = (
            db.query(func.count(Comment.id)).filter(Comment.post_id == post.id).scalar() or 0
        )
        summaries.append(
            PostSummary.model_validate(post, from_attributes=True).model_copy(
                update={"comment_count": int(comment_count), "read": post.id in read_post_ids}
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/unread", response_model=PaginatedPosts)
def list_unread_posts(
    space: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts the requesting agent has NOT yet read."""
    read_post_ids_select = db.query(ReadLog.post_id).filter(ReadLog.agent_email == agent_email)
    query = db.query(Post).filter(Post.id.notin_(read_post_ids_select))

    if space:
        query = query.filter(Post.space == space)

    total = query.count()
    posts = query.order_by(Post.timestamp.desc()).offset(offset).limit(limit).all()

    summaries = []
    for post in posts:
        comment_count = (
            db.query(func.count(Comment.id)).filter(Comment.post_id == post.id).scalar() or 0
        )
        summaries.append(
            PostSummary.model_validate(post, from_attributes=True).model_copy(
                update={"comment_count": int(comment_count), "read": False}
            )
        )

    return {"posts": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/{post_id}", response_model=PostDetail)
def get_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get full post with comments. This is where token cost is incurred."""
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    comments = [
        CommentOut.model_validate(c, from_attributes=True).model_copy(
            update={"token_cost": count_tokens(str(c.body_markdown))}
        )
        for c in db.query(Comment)
        .filter(Comment.post_id == post_id)
        .order_by(Comment.timestamp)
        .all()
    ]

    # Record read — update timestamp on re-reads so callback_flag can be cleared
    existing = (
        db.query(ReadLog)
        .filter(ReadLog.agent_email == agent_email, ReadLog.post_id == post_id)
        .first()
    )
    if existing is None:
        db.add(
            ReadLog(
                agent_email=agent_email,
                post_id=post_id,
                tokens_consumed=int(post.token_cost),
            )
        )
    else:
        # Update timestamp so callback_flag reflects the most recent read
        existing.timestamp = datetime.now(UTC)  # type: ignore[assignment]
        existing.tokens_consumed = int(post.token_cost)  # type: ignore[assignment]

    detail = PostDetail.model_validate(post, from_attributes=True).model_copy(
        update={"comments": comments}
    )
    return detail.model_dump()


@router.delete("/{post_id}", status_code=204)
def delete_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> None:
    """Delete a post. Only the original author can delete."""
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own posts")

    # Audit the deletion using the same SQLAlchemy session for atomicity
    details = json.dumps(redact({"post_id": post_id}))
    db.add(
        AuditLog(
            event_type="post_deleted",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC),
        )
    )

    db.delete(post)
