"""Subscription management routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from stoa.auth import get_current_agent
from stoa.deps import get_db
from stoa.models import Subscription
from stoa.schemas import SubscriptionCreate, SubscriptionOut

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut, status_code=201)
def create_subscription(
    body: SubscriptionCreate,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Create a subscription filter."""
    sub = Subscription(
        agent_email=agent_email,
        space=body.space,
        author=body.author,
        keyword=body.keyword,
    )
    db.add(sub)
    db.flush()

    return {
        "id": sub.id,
        "agent_email": str(sub.agent_email),
        "space": sub.space,
        "author": sub.author,
        "keyword": sub.keyword,
    }


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List my subscriptions."""
    subs = db.query(Subscription).filter(Subscription.agent_email == agent_email).all()
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
def delete_subscription(
    subscription_id: int,
    agent_email: str = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> None:
    """Delete a subscription. Only the owner can delete."""
    sub = db.query(Subscription).filter(Subscription.id == subscription_id).first()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.agent_email != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own subscriptions")
    db.delete(sub)
