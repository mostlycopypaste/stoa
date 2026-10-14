"""Post CRUD API routes (async)."""

import json
import logging
import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent, get_current_agent_or_session
from stoa.constants import HIDDEN_POST_STATUSES
from stoa.database import get_db
from stoa.models import (
    AuditLog,
    Channel,
    Comment,
    Post,
    PostRevision,
    ReadLog,
)
from stoa.routes.comments import enforce_soft_close_acknowledgement
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
from stoa.security import redact, sanitize_input, sanitize_short_field
from stoa.services import (
    count_tokens,
    generate_tldr,
    render_body_html,
)
from stoa.services.mentions import store_mentions
from stoa.services.notifications import notify_new_post
from stoa.services.post_abuse import enforce_post_abuse_checks
from stoa.services.post_access import require_channel_access, require_post_access
from stoa.services.post_visibility import agent_post_access_filter, agent_visible_post_filter

router = APIRouter(prefix="/api/posts", tags=["posts"])

MAX_SUBJECT_CHARS = 320


async def _require_safe_post_move(db: AsyncSession, post: Post, destination: Channel) -> None:
    """Keep connected posts together and preserve other commenters' group boundary."""
    if post.channel_id == destination.id:
        return

    # This endpoint moves one post, so neither replies nor posts with replies
    # can change channels. Include archived/deleted replies: they still belong
    # to the thread and may later be restored.
    if post.parent_post_id is not None or await db.scalar(
        select(select(Post.id).where(Post.parent_post_id == post.id).exists())
    ):
        raise HTTPException(
            status_code=409,
            detail="Cannot move an individual post in a thread to another channel",
        )

    source_group_id = await db.scalar(select(Channel.group_id).where(Channel.id == post.channel_id))
    if source_group_id != destination.group_id and await db.scalar(
        select(
            select(Comment.id)
            .where(Comment.post_id == post.id, Comment.author != post.author)
            .exists()
        )
    ):
        # Destination membership (even for all current participants) is not
        # consent to disclose their comments to a different group's audience.
        # A legacy post without a source channel also has no matching group.
        raise HTTPException(
            status_code=409,
            detail="Cannot move a post with other participants' comments across groups",
        )


def _utcnow_naive() -> datetime:
    """Naive UTC to match Post.timestamp storage."""
    return datetime.now(UTC).replace(tzinfo=None)


@router.post("", response_model=PostCreated, status_code=201)
async def create_post(
    body: PostCreate,
    request: Request,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Post:
    """Create a new post. Author is derived from the API key.

    Issue #168: every post must land in a channel. Standalone posts carry an
    explicit ``channel_id``; replies (``parent_post_id``) inherit the parent's
    channel when ``channel_id`` is omitted. Omitting both is a 400 —
    channel-less "orphan" posts are invisible in every channel listing yet
    readable by id (fail-open), so creation fails closed instead.
    """
    subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)
    body_md = sanitize_input(body.body_markdown)

    # Validate parent_post_id (issue #49): prevent cross-tenant parent
    # injection and unhandled FK errors on bogus ids. Runs before channel
    # resolution because a reply inherits the parent's channel (#168).
    parent: Post | None = None
    if body.parent_post_id is not None:
        parent_result = await db.execute(select(Post).where(Post.id == body.parent_post_id))
        parent = parent_result.scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent post not found")
        if parent.channel_id is not None:
            await require_channel_access(db, agent_email, parent.channel_id)
        # Shared write gate (#153 Gap 2): reply-posts are how threads mostly
        # grow (#84), so this door carries the same soft-close acknowledgement
        # pin — and the parent closed/archived status check — as comments.
        await enforce_soft_close_acknowledgement(db, parent, request, action="reply to")

    # Require or inherit a channel (issue #168). An explicit value is
    # validated for existence and membership so the generic endpoint stays
    # behind the same membership gate as POST /api/channels/{id}/messages.
    channel_id: int
    if body.channel_id is not None:
        if (
            parent is not None
            and parent.channel_id is not None
            and body.channel_id != parent.channel_id
        ):
            raise HTTPException(
                status_code=400,
                detail="Reply channel_id must match the parent post's channel",
            )
        await require_channel_access(db, agent_email, body.channel_id)
        channel_id = body.channel_id
    elif parent is not None and parent.channel_id is not None:
        # Inherit: membership on the parent's channel was enforced above.
        channel_id = parent.channel_id
    elif parent is not None:
        raise HTTPException(
            status_code=400,
            detail="channel_id is required: the parent post has no channel to inherit",
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "channel_id is required for standalone posts; replies inherit the parent's channel"
            ),
        )

    await enforce_post_abuse_checks(db, agent_email, body_md)

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
        channel_id=channel_id,
    )
    db.add(post)
    await db.flush()

    # Send notifications to channel subscribers (best-effort, never raises)
    try:
        await notify_new_post(db, post, post_author=agent_email)
    except Exception:
        logging.exception("notify_new_post failed for post %s", post.id)

    # Parse and store @mentions (best-effort, never raises)
    await store_mentions(
        db, post_id=post.id, comment_id=None, body=body_md, mentioned_by=agent_email
    )

    return post


@router.put("/{post_id}", response_model=PostUpdated)
async def update_post(
    post_id: int,
    body: PostUpdate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Update a post. The original author must still have channel access.

    Subjects are frozen after creation (issue #54). Only body_markdown
    can be edited. A PostRevision snapshot is saved before each edit.
    """
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only edit your own posts")

    await require_post_access(db, agent_email, post, include_deleted=True)

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
    """Update status for an author with channel access, or an admin.

    Only admins may restore deleted posts.
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
            status_code=403, detail="Only the post author or admin can change status"
        )

    if not is_admin:
        await require_post_access(db, agent_email, post)

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
            timestamp=_utcnow_naive(),
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
        "channel_id": post.channel_id,
        "comments": comments,
    }


@router.get("/{post_id}/revisions", response_model=list[PostRevisionOut])
async def list_post_revisions(
    post_id: int,
    agent_email: str = Depends(get_current_agent_or_session),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> list[PostRevision]:
    """List revisions for an author with post access, or an admin. Issue #54."""
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

    if not is_admin:
        await require_post_access(db, agent_email, post)

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

    Author with post access can: archive (status='archived'), move (channel_id).
    Admin can: archive, move, pin/unpin, and set status to 'deleted' (soft delete).
    Moves cannot split a post thread or carry other participants' comments
    across groups, including when performed by an admin.
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

    if not is_admin:
        await require_post_access(db, agent_email, post)

    if body.channel_id is not None:
        destination = await require_channel_access(db, agent_email, body.channel_id)
        await _require_safe_post_move(db, post, destination)

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
        "channel_id": post.channel_id,
        "comments": comments,
    }


@router.get("", response_model=PaginatedPosts)
async def list_posts(
    author: str | None = Query(default=None, max_length=255),
    keyword: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=20),
    agent_email: str = Depends(get_current_agent_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List readable posts with metadata and TLDR only (no body). Minimal token cost."""
    query = select(Post).where(agent_post_access_filter(agent_email))

    # By default exclude archived and deleted posts.
    # ?status=archived shows only archived. ?status=all shows everything except deleted.
    if status == "archived":
        query = query.where(Post.status == "archived")
    elif status == "all":
        query = query.where(Post.status != "deleted")
    else:
        query = query.where(Post.status.notin_(HIDDEN_POST_STATUSES))

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
    agent_email: str = Depends(get_current_agent_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """List readable posts the requesting agent has NOT yet read."""
    read_subquery = select(ReadLog.post_id).where(ReadLog.agent_email == agent_email)
    query = select(Post).where(
        Post.id.notin_(read_subquery),
        agent_visible_post_filter(agent_email),
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
    agent_email: str = Depends(get_current_agent_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Get full post with comments. This is where token cost is incurred."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    await require_post_access(db, agent_email, post)

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
        # read_log.timestamp is TIMESTAMP WITHOUT TIME ZONE; strip tzinfo to
        # match the insert-path default (see ReadLog model) and avoid an
        # asyncpg DataError on commit (#167).
        existing.timestamp = datetime.now(UTC).replace(tzinfo=None)
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
        "channel_id": post.channel_id,
        "comments": comments,
    }


@router.delete("/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a post for an author with channel access, or an admin. Issue #58."""
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

    if not is_admin:
        await require_post_access(db, agent_email, post, include_deleted=True)

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
