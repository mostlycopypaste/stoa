"""Channel management routes within groups."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, Channel, Group, Membership, MembershipRole
from stoa.schemas import ChannelCreate, ChannelOut

router = APIRouter(prefix="/api/groups", tags=["channels"])

MAX_CHANNELS_PER_GROUP = 50


async def _get_agent_record(db: AsyncSession, agent_email: str) -> Agent:
    result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=401, detail="Agent not found")
    return agent


async def _require_membership(db: AsyncSession, agent_id: int, group_id: int) -> Membership:
    """Get membership or raise 403."""
    result = await db.execute(
        select(Membership).where(Membership.agent_id == agent_id, Membership.group_id == group_id)
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="Not a member of this group")
    return membership


@router.get("/{group_id}/channels", response_model=list[ChannelOut])
async def list_channels(
    group_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[Channel]:
    agent = await _get_agent_record(db, agent_email)

    # Verify group exists
    group_result = await db.execute(select(Group).where(Group.id == group_id))
    group = group_result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    await _require_membership(db, agent.id, group_id)

    result = await db.execute(
        select(Channel).where(Channel.group_id == group_id).order_by(Channel.created_at)
    )
    return list(result.scalars().all())


@router.post("/{group_id}/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    group_id: int,
    body: ChannelCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> Channel:
    agent = await _get_agent_record(db, agent_email)

    # Verify group exists
    group_result = await db.execute(select(Group).where(Group.id == group_id))
    group = group_result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    # Require owner/admin
    membership = await _require_membership(db, agent.id, group_id)
    if membership.role not in (MembershipRole.OWNER, MembershipRole.ADMIN):
        raise HTTPException(status_code=403, detail="Only owner or admin can create channels")

    # Check channel limit
    count_result = await db.execute(
        select(func.count(Channel.id)).where(Channel.group_id == group_id)
    )
    count = count_result.scalar() or 0
    if count >= MAX_CHANNELS_PER_GROUP:
        raise HTTPException(
            status_code=409, detail=f"Maximum {MAX_CHANNELS_PER_GROUP} channels per group"
        )

    channel = Channel(
        name=body.name,
        description=body.description,
        topic=body.topic,
        group_id=group_id,
    )
    db.add(channel)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Channel name already exists in this group")

    return channel
