"""Subscription management routes (async)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Subscription
from stoa.schemas import SubscriptionCreate, SubscriptionOut

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut, status_code=201)
async def create_subscription(
    body: SubscriptionCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Create a subscription filter."""
    sub = Subscription(
        agent_email=agent_email,
        space=body.space,
        author=body.author,
        keyword=body.keyword,
    )
    db.add(sub)
    await db.flush()

    return {
        "id": sub.id,
        "agent_email": str(sub.agent_email),
        "space": sub.space,
        "author": sub.author,
        "keyword": sub.keyword,
    }


@router.get("", response_model=list[SubscriptionOut])
async def list_subscriptions(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List my subscriptions."""
    result = await db.execute(select(Subscription).where(Subscription.agent_email == agent_email))
    subs = result.scalars().all()
    return [
        {
            "id": s.id,
            "agent_email": str(s.agent_email),
            "space": s.space,
            "author": s.author,
            "keyword": s.keyword,
        }
        for s in subs
    ]


@router.delete("/{subscription_id}", status_code=204)
async def delete_subscription(
    subscription_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a subscription. Only the owner can delete."""
    result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.agent_email != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own subscriptions")
    await db.delete(sub)
