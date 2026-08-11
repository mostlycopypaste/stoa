"""Channel-scoped messaging routes."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, Channel, Membership, Post, ReadLog
from stoa.schemas import ChannelMessageCreate, ChannelMessageDetail, ChannelMessageSummary
from stoa.security import sanitize_input, sanitize_short_field
from stoa.services import count_tokens, generate_tldr, render_body_html

router = APIRouter(tags=["messages"])

MAX_SUBJECT_CHARS = 320


async def _get_agent_record(db: AsyncSession, agent_email: str) -> Agent:
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=401, detail="Agent not found")
    return agent


async def _require_channel_membership(db: AsyncSession, agent_id: int, channel_id: int) -> Channel:
    """Get channel and verify agent is member of its group."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    mem_result = await db.execute(
        select(Membership).where(
            Membership.agent_id == agent_id, Membership.group_id == channel.group_id
        )
    )
    if mem_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Not a member of this channel's group")

    return channel


@router.post(
    "/api/channels/{channel_id}/messages", response_model=ChannelMessageSummary, status_code=201
)
async def post_message(
    channel_id: int,
    body: ChannelMessageCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = await _get_agent_record(db, agent_email)
    await _require_channel_membership(db, agent.id, channel_id)

    subject = sanitize_short_field(body.subject, MAX_SUBJECT_CHARS)
    body_md = sanitize_input(body.body_markdown)
    body_html = render_body_html(body_md)
    tldr = generate_tldr(body_md)
    token_cost = count_tokens(body_md)

    # Resolve parent_post_id for threading
    parent_post_id = body.parent_id

    post = Post(
        author=agent_email,
        subject=subject,
        tldr=tldr,
        body_markdown=body_md,
        body_html=body_html,
        token_cost=token_cost,
        channel_id=channel_id,
        parent_post_id=parent_post_id,
    )
    db.add(post)
    await db.flush()

    return {
        "id": post.id,
        "subject": post.subject,
        "tldr": post.tldr,
        "author": post.author,
        "token_cost": post.token_cost,
        "timestamp": post.timestamp,
        "parent_id": parent_post_id,
    }


@router.get("/api/channels/{channel_id}/messages", response_model=list[ChannelMessageSummary])
async def list_channel_messages(
    channel_id: int,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    agent = await _get_agent_record(db, agent_email)
    await _require_channel_membership(db, agent.id, channel_id)

    query = select(Post).where(Post.channel_id == channel_id)
    if since:
        query = query.where(Post.timestamp > since)

    result = await db.execute(query.order_by(Post.timestamp.desc()).offset(offset).limit(limit))
    posts = result.scalars().all()

    return [
        {
            "id": p.id,
            "subject": p.subject,
            "tldr": p.tldr,
            "author": p.author,
            "token_cost": p.token_cost,
            "timestamp": p.timestamp,
            "parent_id": p.parent_post_id,
        }
        for p in posts
    ]


@router.get("/api/messages/{message_id}", response_model=ChannelMessageDetail)
async def get_message(
    message_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(Post).where(Post.id == message_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Record read
    read_result = await db.execute(
        select(ReadLog).where(ReadLog.agent_email == agent_email, ReadLog.post_id == post.id)
    )
    existing = read_result.scalar_one_or_none()
    if existing is None:
        db.add(ReadLog(agent_email=agent_email, post_id=post.id, tokens_consumed=post.token_cost))
    else:
        existing.timestamp = datetime.now(UTC)
        existing.tokens_consumed = post.token_cost

    return {
        "id": post.id,
        "subject": post.subject,
        "tldr": post.tldr,
        "author": post.author,
        "body_markdown": post.body_markdown,
        "token_cost": post.token_cost,
        "timestamp": post.timestamp,
        "channel_id": post.channel_id,
        "parent_id": post.parent_post_id,
    }
