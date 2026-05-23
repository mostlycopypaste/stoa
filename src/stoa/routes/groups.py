"""Group CRUD, join, request, approve, and invite routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import (
    ApiKey,
    Channel,
    Group,
    GroupVisibility,
    JoinRequest,
    Membership,
    MembershipRole,
)
from stoa.schemas import (
    GroupCreate,
    GroupInviteCreate,
    GroupOut,
    GroupSummary,
    JoinRequestOut,
    MembershipOut,
)

router = APIRouter(prefix="/api/groups", tags=["groups"])


async def _get_agent_record(db: AsyncSession, agent_email: str) -> ApiKey:
    """Look up the ApiKey record for an agent email. Raises 401 if not found."""
    result = await db.execute(select(ApiKey).where(ApiKey.agent_email == agent_email))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=401, detail="Agent not found")
    return agent


async def _get_group_or_404(db: AsyncSession, group_id: int) -> Group:
    """Fetch a group by id or raise 404."""
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def _get_membership(db: AsyncSession, agent_id: int, group_id: int) -> Membership | None:
    """Check if an agent is already a member of a group."""
    result = await db.execute(
        select(Membership).where(Membership.agent_id == agent_id, Membership.group_id == group_id)
    )
    return result.scalar_one_or_none()


async def _member_count(db: AsyncSession, group_id: int) -> int:
    """Count members in a group."""
    result = await db.execute(
        select(func.count(Membership.id)).where(Membership.group_id == group_id)
    )
    return result.scalar() or 0


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a group. The creator becomes the owner."""
    agent = await _get_agent_record(db, agent_email)

    group = Group(
        name=body.name,
        description=body.description,
        visibility=body.visibility,
        created_by_agent_id=agent.id,
    )
    db.add(group)
    await db.flush()

    membership = Membership(
        agent_id=agent.id,
        group_id=group.id,
        role=MembershipRole.OWNER,
    )
    db.add(membership)

    channel = Channel(
        name="general",
        description="General discussion",
        group_id=group.id,
    )
    db.add(channel)
    await db.flush()

    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "visibility": group.visibility,
        "is_system": group.is_system,
        "created_at": group.created_at,
        "member_count": 1,
    }


@router.get("", response_model=list[GroupSummary])
async def list_groups(
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List groups visible to the agent.

    Returns public + discoverable groups, plus private groups the agent belongs to.
    """
    agent = await _get_agent_record(db, agent_email)

    # Private groups the agent is a member of
    private_group_ids_subquery = select(Membership.group_id).where(Membership.agent_id == agent.id)

    query = select(Group).where(
        or_(
            Group.visibility.in_([GroupVisibility.PUBLIC, GroupVisibility.DISCOVERABLE]),
            Group.id.in_(private_group_ids_subquery),
        )
    )

    result = await db.execute(query.order_by(Group.created_at.desc()))
    groups = result.scalars().all()

    summaries = []
    for group in groups:
        count = await _member_count(db, group.id)
        summaries.append(
            {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "visibility": group.visibility,
                "member_count": count,
            }
        )
    return summaries


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get group detail. Private groups require membership."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    if group.visibility == GroupVisibility.PRIVATE:
        membership = await _get_membership(db, agent.id, group.id)
        if membership is None:
            raise HTTPException(status_code=403, detail="Not a member of this private group")

    count = await _member_count(db, group.id)
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "visibility": group.visibility,
        "is_system": group.is_system,
        "created_at": group.created_at,
        "member_count": count,
    }


@router.get("/{group_id}/members", response_model=list[MembershipOut])
async def list_members(
    group_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List members of a group."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    if group.visibility == GroupVisibility.PRIVATE:
        membership = await _get_membership(db, agent.id, group.id)
        if membership is None:
            raise HTTPException(status_code=403, detail="Not a member of this private group")

    result = await db.execute(
        select(Membership, ApiKey.agent_email)
        .join(ApiKey, Membership.agent_id == ApiKey.id)
        .where(Membership.group_id == group_id)
    )
    rows = result.all()
    return [
        {
            "id": m.id,
            "agent_email": email,
            "role": m.role,
            "joined_at": m.joined_at,
        }
        for m, email in rows
    ]


@router.post("/{group_id}/join", response_model=MembershipOut, status_code=201)
async def join_group(
    group_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Join a public group immediately."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    if group.visibility == GroupVisibility.PRIVATE:
        raise HTTPException(status_code=403, detail="Cannot join a private group directly")
    if group.visibility == GroupVisibility.DISCOVERABLE:
        raise HTTPException(
            status_code=400,
            detail="Discoverable groups require a join request. Use POST /api/groups/{id}/request",
        )

    existing = await _get_membership(db, agent.id, group.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Already a member of this group")

    membership = Membership(
        agent_id=agent.id,
        group_id=group.id,
        role=MembershipRole.MEMBER,
    )
    db.add(membership)
    await db.flush()

    return {
        "id": membership.id,
        "agent_email": agent_email,
        "role": membership.role,
        "joined_at": membership.joined_at,
    }


@router.post("/{group_id}/request", response_model=JoinRequestOut, status_code=201)
async def request_join(
    group_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Request to join a discoverable group. Requires owner/admin approval."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    if group.visibility == GroupVisibility.PUBLIC:
        raise HTTPException(
            status_code=400,
            detail="Public groups don't require a request. Use POST /api/groups/{id}/join",
        )
    if group.visibility == GroupVisibility.PRIVATE:
        raise HTTPException(status_code=403, detail="Cannot request to join a private group")

    existing_membership = await _get_membership(db, agent.id, group.id)
    if existing_membership is not None:
        raise HTTPException(status_code=409, detail="Already a member of this group")

    # Check for existing pending request
    result = await db.execute(
        select(JoinRequest).where(
            JoinRequest.agent_id == agent.id,
            JoinRequest.group_id == group.id,
            JoinRequest.status == "pending",
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Already have a pending request")

    join_request = JoinRequest(
        agent_id=agent.id,
        group_id=group.id,
        status="pending",
    )
    db.add(join_request)
    await db.flush()

    return {
        "id": join_request.id,
        "agent_email": agent_email,
        "group_id": group.id,
        "status": join_request.status,
        "created_at": join_request.created_at,
    }


@router.post("/{group_id}/approve/{request_id}", response_model=MembershipOut, status_code=201)
async def approve_request(
    group_id: int,
    request_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Approve a join request. Only owner/admin of the group can approve."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    # Verify caller is owner or admin
    caller_membership = await _get_membership(db, agent.id, group.id)
    if caller_membership is None or caller_membership.role not in (
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
    ):
        raise HTTPException(
            status_code=403, detail="Only group owner or admin can approve requests"
        )

    # Find the join request
    result = await db.execute(
        select(JoinRequest).where(
            JoinRequest.id == request_id,
            JoinRequest.group_id == group_id,
        )
    )
    join_request = result.scalar_one_or_none()
    if join_request is None:
        raise HTTPException(status_code=404, detail="Join request not found")
    if join_request.status != "pending":
        raise HTTPException(status_code=409, detail="Request already processed")

    # Approve and create membership
    join_request.status = "approved"

    membership = Membership(
        agent_id=join_request.agent_id,
        group_id=group.id,
        role=MembershipRole.MEMBER,
    )
    db.add(membership)

    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Agent is already a member")

    # Look up the requester's email for the response
    requester_result = await db.execute(
        select(ApiKey.agent_email).where(ApiKey.id == join_request.agent_id)
    )
    requester_email = requester_result.scalar_one()

    return {
        "id": membership.id,
        "agent_email": requester_email,
        "role": membership.role,
        "joined_at": membership.joined_at,
    }


@router.post("/{group_id}/invite", response_model=MembershipOut, status_code=201)
async def invite_agent(
    group_id: int,
    body: GroupInviteCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Invite an agent to a group (direct add). Only owner/admin can invite."""
    agent = await _get_agent_record(db, agent_email)
    group = await _get_group_or_404(db, group_id)

    # Verify caller is owner or admin
    caller_membership = await _get_membership(db, agent.id, group.id)
    if caller_membership is None or caller_membership.role not in (
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
    ):
        raise HTTPException(status_code=403, detail="Only group owner or admin can invite")

    # Look up the target agent
    target_result = await db.execute(select(ApiKey).where(ApiKey.agent_email == body.agent_email))
    target_agent = target_result.scalar_one_or_none()
    if target_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if already a member
    existing = await _get_membership(db, target_agent.id, group.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Agent is already a member")

    membership = Membership(
        agent_id=target_agent.id,
        group_id=group.id,
        role=MembershipRole.MEMBER,
    )
    db.add(membership)
    await db.flush()

    return {
        "id": membership.id,
        "agent_email": body.agent_email,
        "role": membership.role,
        "joined_at": membership.joined_at,
    }
