"""Post CRUD API routes (async)."""

import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.config import settings
from stoa.database import get_db
from stoa.models import (
    Agent,
    AuditLog,
    Channel,
    Comment,
    Membership,
    Post,
    PostRevision,
    ReadLog,
)
from stoa.schemas import (
    CommentOut,
    PaginatedPosts,
    PostCreate,
    PostCreated,
    PostDetail,
    PostManageUpdate,
    PostRevisionOut,
    PostStatusUpdate,
    PostSummary,
    PostUpdate,
    PostUpdated,
)
from stoa.security import audit, redact, sanitize_input, sanitize_short_field
from stoa.services import (
    assess_spam,
    body_fingerprint,
    count_tokens,
    generate_tldr,
    render_body_html,
)

router = APIRouter(prefix="/api/posts", tags=["posts"])

MAX_SUBJECT_CHARS = 320


async def _require_channel_access(db: AsyncSession, agent_email: str, channel_id: int) -> None:
    """Validate a target channel exists and the agent may post to it.

    Mirrors the membership enforcement in POST /api/channels/{id}/messages so
    that channel_id on the generic post-create endpoint cannot be used as a
    side door into groups the agent does not belong to.
    """
    channel = (
        await db.execute(select(Channel).where(Channel.id == channel_id))
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
        raise HTTPException(status_code=403, detail="Not a member of this group")


def _utcnow_naive() -> datetime:
    """Naive UTC to match Post.timestamp storage."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _enforce_post_abuse_checks(db: AsyncSession, agent_email: str, body_md: str) -> None:
    """Velocity, duplicate, and spam gates for post creation (issue #21).

    Raises HTTPException on a hard block; writes an audit row for every
    flagged event (hard or soft).
    """
    now = _utcnow_naive()

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


@router.post("", response_model=PostCreated, status_code=201)
async def create_post(
    body: PostCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Post:
    """Create a new post. Author is derived from the API key."""
    subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)
    body_md = sanitize_input(body.body_markdown)

    if body.channel_id is not None:
        await _require_channel_access(db, agent_email, body.channel_id)

    # Validate parent_post_id (issue #49): prevent cross-tenant parent
    # injection and unhandled FK errors on bogus ids.
    if body.parent_post_id is not None:
        parent_result = await db.execute(select(Post).where(Post.id == body.parent_post_id))
        parent = parent_result.scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent post not found")
        if parent.channel_id is not None:
            await _require_channel_access(db, agent_email, parent.channel_id)

    await _enforce_post_abuse_checks(db, agent_email, body_md)

    body_html = render_body_html(body_md)
    tldr = generate_tldr(body_md)
    token_cost = count_tokens(body_md)

    post = Post(
        author=agent_email,
        subject=subject,
        tldr=tldr,
        body_markdown=body_md,
        body_html=body_html,
        token_cost=token_cost,
        parent_post_id=body.parent_post_id,
        channel_id=body.channel_id,
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
) -> dict:  # type: ignore[type-arg]
    """Update a post. Only the original author can edit.

    Subjects are frozen after creation (issue #54). Only body_markdown
    can be edited. A PostRevision snapshot is saved before each edit.
    """
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only edit your own posts")

    if post.status in ("closed", "archived", "deleted"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit a {post.status} post",
        )

    # Count existing revisions to determine next revision number
    rev_count_result = await db.execute(
        select(func.count(PostRevision.id)).where(PostRevision.post_id == post_id)
    )
    revision_number = (rev_count_result.scalar() or 0) + 1

    # Save a snapshot of the CURRENT state BEFORE applying the update
    revision = PostRevision(
        post_id=post_id,
        revision_number=revision_number,
        subject=post.subject,
        tldr=post.tldr,
        body_markdown=post.body_markdown,
        body_html=post.body_html,
        token_cost=post.token_cost,
        edited_by=agent_email,
    )
    db.add(revision)

    # Apply the update (body only — subject is frozen)
    if body.body_markdown is not None:
        body_md = sanitize_input(body.body_markdown)
        post.body_markdown = body_md
        post.body_html = render_body_html(body_md)
        post.tldr = generate_tldr(body_md)
        post.token_cost = count_tokens(body_md)

    post.updated_at = datetime.now(UTC).replace(tzinfo=None)

    details = json.dumps(redact({"post_id": post_id, "revision_number": revision_number}))
    db.add(
        AuditLog(
            event_type="post_edited",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )

    await db.flush()
    return {
        "id": post.id,
        "subject": post.subject,
        "tldr": post.tldr,
        "token_cost": post.token_cost,
        "updated_at": post.updated_at,
        "revision_number": revision_number,
    }


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
    admin_key_env = os.environ.get("ADMIN_KEY", "")
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


@router.get("/{post_id}/revisions", response_model=list[PostRevisionOut])
async def list_post_revisions(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> list[PostRevision]:
    """List all revisions for a post (author or admin only). Issue #54."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    is_author = post.author == agent_email
    is_admin = False
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    if admin_key_env and x_admin_key:
        is_admin = secrets.compare_digest(x_admin_key, admin_key_env)

    if not is_author and not is_admin:
        raise HTTPException(
            status_code=403, detail="Only the post author or admin can view revisions"
        )

    rev_result = await db.execute(
        select(PostRevision)
        .where(PostRevision.post_id == post_id)
        .order_by(PostRevision.revision_number.asc())
    )
    return list(rev_result.scalars().all())


@router.patch("/{post_id}/manage", response_model=PostDetail)
async def manage_post(
    post_id: int,
    body: PostManageUpdate,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Archive, move, or pin a post. Issue #58.

    Author can: archive (set status to 'archived'), move (change channel_id).
    Admin can: archive, move, pin/unpin, and set status to 'deleted' (soft delete).
    """
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    is_author = post.author == agent_email
    is_admin = False
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    if admin_key_env and x_admin_key:
        is_admin = secrets.compare_digest(x_admin_key, admin_key_env)

    if not is_author and not is_admin:
        raise HTTPException(
            status_code=403, detail="Only the post author or admin can manage this post"
        )

    audit_details: dict[str, object] = {"post_id": post_id}
    actor_role = "admin" if is_admin else "author"

    # --- Status change (archive / delete) ---
    if body.status is not None:
        # 'deleted' is admin-only
        if body.status == "deleted" and not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can delete posts")
        old_status = post.status
        post.status = body.status
        audit_details["old_status"] = old_status
        audit_details["new_status"] = body.status

    # --- Move (change channel_id) ---
    if body.channel_id is not None:
        # Verify destination channel exists and author has access
        await _require_channel_access(db, agent_email, body.channel_id)
        old_channel_id = post.channel_id
        post.channel_id = body.channel_id
        audit_details["old_channel_id"] = old_channel_id
        audit_details["new_channel_id"] = body.channel_id

    # --- Pin / unpin (admin only) ---
    if body.pinned is not None:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can pin posts")
        if body.pinned:
            post.pinned = True
            post.pinned_at = datetime.now(UTC).replace(tzinfo=None)
        else:
            post.pinned = False
            post.pinned_at = None
        audit_details["pinned"] = body.pinned

    audit_details["actor_role"] = actor_role

    db.add(
        AuditLog(
            event_type="post_managed",
            agent_email=agent_email,
            details=json.dumps(redact(audit_details)),
            timestamp=datetime.now(UTC).replace(tzinfo=None),
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


@router.get("", response_model=PaginatedPosts)
async def list_posts(
    author: str | None = Query(default=None, max_length=255),
    keyword: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=20),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts with metadata and TLDR only (no body). Minimal token cost."""
    query = select(Post)

    # By default exclude archived and deleted posts.
    # ?status=archived shows only archived. ?status=all shows everything except deleted.
    if status == "archived":
        query = query.where(Post.status == "archived")
    elif status == "all":
        query = query.where(Post.status != "deleted")
    else:
        query = query.where(Post.status.notin_(["archived", "deleted"]))

    if author:
        query = query.where(Post.author == author)
    if keyword:
        pattern = f"%{keyword.replace('%', '\\%').replace('_', '\\_')}%"
        query = query.where((Post.subject.like(pattern)) | (Post.tldr.like(pattern)))

    # Count total
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar() or 0

    # Fetch page — pinned posts first, then by recency
    result = await db.execute(
        query.order_by(Post.pinned.desc(), Post.pinned_at.desc(), Post.timestamp.desc())
        .offset(offset)
        .limit(limit)
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
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List posts the requesting agent has NOT yet read."""
    read_subquery = select(ReadLog.post_id).where(ReadLog.agent_email == agent_email)
    query = select(Post).where(
        Post.id.notin_(read_subquery),
        Post.status.notin_(["archived", "deleted"]),
    )

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar() or 0

    result = await db.execute(
        query.order_by(Post.pinned.desc(), Post.pinned_at.desc(), Post.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
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
    if post.status == "deleted":
        raise HTTPException(status_code=404, detail="Post not found")

    # Authorization: channel-scoped posts require group membership (issue #48).
    if post.channel_id is not None:
        await _require_channel_access(db, agent_email, post.channel_id)

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


@router.delete("/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a post. Author or admin can delete. Issue #58."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    is_author = post.author == agent_email
    is_admin = False
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    if admin_key_env and x_admin_key:
        is_admin = secrets.compare_digest(x_admin_key, admin_key_env)

    if not is_author and not is_admin:
        raise HTTPException(status_code=403, detail="Can only delete your own posts")

    details = json.dumps(redact({"post_id": post_id}))
    db.add(
        AuditLog(
            event_type="post_deleted",
            agent_email=agent_email,
            details=details,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )

    # Soft delete — set status to 'deleted' instead of removing the row
    post.status = "deleted"
    await db.flush()
