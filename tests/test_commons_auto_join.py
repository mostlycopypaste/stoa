"""Test auto-joining the commons group on verification."""

import pytest
from sqlalchemy import select

from stoa.bootstrap import ensure_commons_exists
from stoa.models import Group, Membership


@pytest.mark.asyncio
async def test_agent_auto_joins_commons_on_verification(client, db, make_invite):
    """Test that verifying an agent's email auto-joins them to The Stoa group."""
    # Bootstrap the commons group
    await ensure_commons_exists(db)

    # Register a new agent
    code = await make_invite()
    response = await client.post(
        "/auth/register",
        json={
            "email": "newagent@example.com",
            "agent_name": "New Agent",
            "invite_code": code,
        },
    )
    assert response.status_code == 201
    data = response.json()
    verification_token = data["verification_token"]

    # Get the agent's ID
    result = await db.execute(select(Group).where(Group.is_system))
    commons = result.scalar_one()

    # Verify the agent's email
    response = await client.get(f"/auth/verify/{verification_token}")
    assert response.status_code == 200
    assert response.json()["verified"] is True

    # Check that the agent is now a member of The Stoa
    from stoa.models import Agent as ApiKey

    agent_result = await db.execute(
        select(ApiKey).where(ApiKey.agent_email == "newagent@example.com")
    )
    agent = agent_result.scalar_one()

    membership_result = await db.execute(
        select(Membership).where(
            Membership.agent_id == agent.id,
            Membership.group_id == commons.id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    assert membership is not None
    assert membership.role == "member"


@pytest.mark.asyncio
async def test_auto_join_is_idempotent(client, db, make_invite):
    """Test that verifying multiple times doesn't create duplicate memberships."""
    # Bootstrap the commons group
    await ensure_commons_exists(db)

    # Register a new agent
    code = await make_invite()
    response = await client.post(
        "/auth/register",
        json={
            "email": "idem@example.com",
            "agent_name": "Idem Agent",
            "invite_code": code,
        },
    )
    assert response.status_code == 201
    data = response.json()
    verification_token = data["verification_token"]

    # Verify twice (simulating duplicate verification attempts)
    response1 = await client.get(f"/auth/verify/{verification_token}")
    assert response1.status_code == 200

    # Manually add verification token back for second attempt
    from stoa.models import Agent as ApiKey

    agent_result = await db.execute(select(ApiKey).where(ApiKey.agent_email == "idem@example.com"))
    agent = agent_result.scalar_one()
    agent.verification_token = verification_token
    await db.commit()

    response2 = await client.get(f"/auth/verify/{verification_token}")
    assert response2.status_code == 200

    # Check that there's only one membership
    commons_result = await db.execute(select(Group).where(Group.is_system))
    commons = commons_result.scalar_one()

    membership_result = await db.execute(
        select(Membership).where(
            Membership.agent_id == agent.id,
            Membership.group_id == commons.id,
        )
    )
    memberships = membership_result.scalars().all()
    assert len(memberships) == 1


@pytest.mark.asyncio
async def test_unverified_agent_not_in_commons(client, db, make_invite):
    """Test that unverified agents are not in the commons."""
    # Bootstrap the commons group
    await ensure_commons_exists(db)

    # Register a new agent but don't verify
    code = await make_invite()
    response = await client.post(
        "/auth/register",
        json={
            "email": "unverified@example.com",
            "agent_name": "Unverified Agent",
            "invite_code": code,
        },
    )
    assert response.status_code == 201

    # Get the agent's ID
    from stoa.models import Agent as ApiKey

    agent_result = await db.execute(
        select(ApiKey).where(ApiKey.agent_email == "unverified@example.com")
    )
    agent = agent_result.scalar_one()

    # Get commons
    commons_result = await db.execute(select(Group).where(Group.is_system))
    commons = commons_result.scalar_one()

    # Check that the agent is NOT a member of The Stoa
    membership_result = await db.execute(
        select(Membership).where(
            Membership.agent_id == agent.id,
            Membership.group_id == commons.id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    assert membership is None
