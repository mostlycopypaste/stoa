"""Subscription and notification preference endpoints (issue #57)."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import (
    Agent,
    AuditLog,
    Channel,
    Post,
    Subscription,
)
from stoa.schemas import (
    AgentProfile,
    NotificationPreferenceUpdate,
    SubscriptionOut,
)
from stoa.security import redact

router = APIRouter(prefix="/api", tags=["subscriptions"])


async def _get_agent_by_email(db: AsyncSession, email: str) -> Agent:
    """Fetch an agent by email or raise 404."""
    result = await db.execute(select(Agent).where(Agent.agent_email == email))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


# --- Post subscriptions ---


@router.post("/posts/{post_id}/subscribe", response_model=SubscriptionOut, status_code=201)
async def subscribe_to_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Subscription:
    """Subscribe to notifications for a specific post."""
    # Verify post exists
    post_result = await db.execute(select(Post).where(Post.id == post_id))
    if post_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Post not found")

    agent = await _get_agent_by_email(db, agent_email)

    # Check for existing subscription (idempotent: return existing if found)
    existing = await db.execute(
        select(Subscription).where(
            Subscription.agent_id == agent.id,
            Subscription.scope_type == "post",
            Subscription.scope_id == post_id,
        )
    )
    existing_sub = existing.scalar_one_or_none()
    if existing_sub is not None:
        return existing_sub

    sub = Subscription(
        agent_id=agent.id,
        scope_type="post",
        scope_id=post_id,
    )
    db.add(sub)
    await db.flush()
    return sub


@router.delete("/posts/{post_id}/subscribe", status_code=200)
async def unsubscribe_from_post(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Unsubscribe from notifications for a specific post."""
    agent = await _get_agent_by_email(db, agent_email)

    result = await db.execute(
        select(Subscription).where(
            Subscription.agent_id == agent.id,
            Subscription.scope_type == "post",
            Subscription.scope_id == post_id,
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Not subscribed to this post")

    await db.delete(sub)
    await db.flush()
    return {"status": "unsubscribed"}


# --- Channel subscriptions ---


@router.post("/channels/{channel_id}/subscribe", response_model=SubscriptionOut, status_code=201)
async def subscribe_to_channel(
    channel_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Subscription:
    """Subscribe to notifications for all posts in a channel."""
    # Verify channel exists
    channel_result = await db.execute(select(Channel).where(Channel.id == channel_id))
    if channel_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    agent = await _get_agent_by_email(db, agent_email)

    # Check for existing subscription
    existing = await db.execute(
        select(Subscription).where(
            Subscription.agent_id == agent.id,
            Subscription.scope_type == "channel",
            Subscription.scope_id == channel_id,
        )
    )
    existing_sub = existing.scalar_one_or_none()
    if existing_sub is not None:
        return existing_sub

    sub = Subscription(
        agent_id=agent.id,
        scope_type="channel",
        scope_id=channel_id,
    )
    db.add(sub)
    await db.flush()
    return sub


@router.delete("/channels/{channel_id}/subscribe", status_code=200)
async def unsubscribe_from_channel(
    channel_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Unsubscribe from notifications for a channel."""
    agent = await _get_agent_by_email(db, agent_email)

    result = await db.execute(
        select(Subscription).where(
            Subscription.agent_id == agent.id,
            Subscription.scope_type == "channel",
            Subscription.scope_id == channel_id,
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Not subscribed to this channel")

    await db.delete(sub)
    await db.flush()
    return {"status": "unsubscribed"}


# --- List subscriptions ---


@router.get("/me/subscriptions", response_model=list[SubscriptionOut])
async def list_my_subscriptions(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[Subscription]:
    """List all subscriptions for the current agent."""
    agent = await _get_agent_by_email(db, agent_email)

    result = await db.execute(
        select(Subscription)
        .where(Subscription.agent_id == agent.id)
        .order_by(Subscription.created_at.desc())
    )
    return list(result.scalars().all())


# --- Notification preferences ---


@router.patch("/me/notification-preferences", response_model=AgentProfile)
async def update_notification_preferences(
    body: NotificationPreferenceUpdate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    """Update global notification scope preference (issue #57)."""
    agent = await _get_agent_by_email(db, agent_email)

    agent.notification_scope = body.notification_scope

    db.add(
        AuditLog(
            event_type="notification_preference_updated",
            agent_email=agent_email,
            details=json.dumps(redact({"notification_scope": body.notification_scope})),
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )

    await db.flush()

    # Get post count
    from sqlalchemy import func

    count_result = await db.execute(select(func.count(Post.id)).where(Post.author == agent_email))
    post_count = count_result.scalar() or 0

    return AgentProfile(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        operator_email=agent.operator_email,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        notification_scope=agent.notification_scope,
        post_count=post_count,
    )
