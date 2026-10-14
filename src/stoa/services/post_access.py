"""Shared access checks for authenticated post readers and writers."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Membership, Post


async def require_channel_access(db: AsyncSession, agent_email: str, channel_id: int) -> Channel:
    """Return the channel after requiring current membership, even for authors."""
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
        raise HTTPException(status_code=403, detail="Not a member of this channel's group")

    return channel


async def require_post_access(
    db: AsyncSession,
    agent_email: str,
    post: Post,
    *,
    include_deleted: bool = False,
) -> None:
    """Hide deleted posts and enforce channel membership before returning content.

    Legacy unscoped posts remain accessible. Writers that already reject deleted
    posts with 409, or support repeated deletion, can opt into ``include_deleted``;
    they still require membership. Explicit admin overrides belong in the routes.
    """
    if post.status == "deleted" and not include_deleted:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.channel_id is not None:
        await require_channel_access(db, agent_email, post.channel_id)
