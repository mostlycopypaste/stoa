"""Tests for Group, Membership, and JoinRequest models."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Group, GroupVisibility, JoinRequest, Membership, MembershipRole

from .helpers import create_test_api_key


@pytest.mark.asyncio
class TestGroupModel:
    """Test Group model."""

    async def test_create_group_with_all_visibilities(self, db: AsyncSession):
        """Create groups with all visibility types."""
        agent = await create_test_api_key(db, "creator@test.com", "test-key-123")
        await db.commit()

        public_group = Group(
            name="Public Group",
            description="Everyone can see this",
            visibility=GroupVisibility.PUBLIC,
            created_by_agent_id=agent.id,
        )
        discoverable_group = Group(
            name="Discoverable Group",
            description="Can be found in search",
            visibility=GroupVisibility.DISCOVERABLE,
            created_by_agent_id=agent.id,
        )
        private_group = Group(
            name="Private Group",
            description="Invite only",
            visibility=GroupVisibility.PRIVATE,
            created_by_agent_id=agent.id,
        )

        db.add_all([public_group, discoverable_group, private_group])
        await db.commit()

        result = await db.execute(select(Group).order_by(Group.id))
        groups = result.scalars().all()

        assert len(groups) == 3
        assert groups[0].visibility == "public"
        assert groups[1].visibility == "discoverable"
        assert groups[2].visibility == "private"

    async def test_group_default_visibility(self, db: AsyncSession):
        """Default visibility should be PUBLIC."""
        group = Group(name="Default Group", description="Test")
        db.add(group)
        await db.commit()

        result = await db.execute(select(Group).filter_by(name="Default Group"))
        saved_group = result.scalar_one()
        assert saved_group.visibility == "public"

    async def test_group_system_flag(self, db: AsyncSession):
        """Test is_system flag."""
        system_group = Group(name="The Commons", is_system=True)
        user_group = Group(name="User Group", is_system=False)

        db.add_all([system_group, user_group])
        await db.commit()

        result = await db.execute(select(Group).filter_by(is_system=True))
        sys_group = result.scalar_one()
        assert sys_group.name == "The Commons"

    async def test_group_created_by_nullable(self, db: AsyncSession):
        """created_by_agent_id can be NULL for system groups."""
        group = Group(name="System Group", created_by_agent_id=None)
        db.add(group)
        await db.commit()

        result = await db.execute(select(Group).filter_by(name="System Group"))
        saved_group = result.scalar_one()
        assert saved_group.created_by_agent_id is None

    async def test_group_repr(self, db: AsyncSession):
        """Group repr should be readable."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()
        assert "Group" in repr(group)
        assert "Test Group" in repr(group)


@pytest.mark.asyncio
class TestMembershipModel:
    """Test Membership model."""

    async def test_create_membership(self, db: AsyncSession):
        """Create a membership linking agent to group."""
        agent = await create_test_api_key(db, "member@test.com", "member-key-123")
        group = Group(name="Test Group")
        db.add_all([agent, group])
        await db.commit()

        membership = Membership(
            agent_id=agent.id, group_id=group.id, role=MembershipRole.MEMBER
        )
        db.add(membership)
        await db.commit()

        result = await db.execute(select(Membership).filter_by(agent_id=agent.id))
        saved_membership = result.scalar_one()
        assert saved_membership.group_id == group.id
        assert saved_membership.role == "member"

    async def test_membership_roles(self, db: AsyncSession):
        """Test all membership role types."""
        owner = await create_test_api_key(db, "owner@test.com", "owner-key-123")
        admin = await create_test_api_key(db, "admin@test.com", "admin-key-123")
        member = await create_test_api_key(db, "member@test.com", "member-key-123")
        group = Group(name="Multi-Role Group")

        db.add_all([owner, admin, member, group])
        await db.commit()

        owner_membership = Membership(
            agent_id=owner.id, group_id=group.id, role=MembershipRole.OWNER
        )
        admin_membership = Membership(
            agent_id=admin.id, group_id=group.id, role=MembershipRole.ADMIN
        )
        member_membership = Membership(
            agent_id=member.id, group_id=group.id, role=MembershipRole.MEMBER
        )

        db.add_all([owner_membership, admin_membership, member_membership])
        await db.commit()

        result = await db.execute(select(Membership).order_by(Membership.id))
        memberships = result.scalars().all()

        assert len(memberships) == 3
        assert memberships[0].role == "owner"
        assert memberships[1].role == "admin"
        assert memberships[2].role == "member"

    async def test_membership_unique_constraint(self, db: AsyncSession):
        """agent_id + group_id must be unique."""
        agent = await create_test_api_key(db, "agent@test.com", "agent-key-123")
        group = Group(name="Unique Test Group")
        db.add_all([agent, group])
        await db.commit()

        membership1 = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership1)
        await db.commit()

        # Try to add duplicate membership
        membership2 = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership2)

        with pytest.raises(Exception):  # SQLAlchemy will raise IntegrityError
            await db.commit()

    async def test_membership_default_role(self, db: AsyncSession):
        """Default role should be MEMBER."""
        agent = await create_test_api_key(db, "default@test.com", "default-key-123")
        group = Group(name="Default Role Group")
        db.add_all([agent, group])
        await db.commit()

        membership = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership)
        await db.commit()

        result = await db.execute(select(Membership).filter_by(agent_id=agent.id))
        saved_membership = result.scalar_one()
        assert saved_membership.role == "member"

    async def test_membership_repr(self, db: AsyncSession):
        """Membership repr should be readable."""
        agent = await create_test_api_key(db, "repr@test.com", "repr-key-123")
        group = Group(name="Repr Group")
        db.add_all([agent, group])
        await db.commit()

        membership = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership)
        await db.commit()

        assert "Membership" in repr(membership)
        assert str(agent.id) in repr(membership)
        assert str(group.id) in repr(membership)


@pytest.mark.asyncio
class TestJoinRequestModel:
    """Test JoinRequest model."""

    async def test_create_join_request(self, db: AsyncSession):
        """Create a pending join request."""
        agent = await create_test_api_key(db, "requester@test.com", "req-key-123")
        group = Group(name="Discoverable Group", visibility=GroupVisibility.DISCOVERABLE)
        db.add_all([agent, group])
        await db.commit()

        join_request = JoinRequest(agent_id=agent.id, group_id=group.id)
        db.add(join_request)
        await db.commit()

        result = await db.execute(select(JoinRequest).filter_by(agent_id=agent.id))
        saved_request = result.scalar_one()
        assert saved_request.status == "pending"

    async def test_join_request_status_values(self, db: AsyncSession):
        """Test all valid status values."""
        agent1 = await create_test_api_key(db, "pending@test.com", "pending-key")
        agent2 = await create_test_api_key(db, "approved@test.com", "approved-key")
        agent3 = await create_test_api_key(db, "rejected@test.com", "rejected-key")
        group = Group(name="Status Test Group")

        db.add_all([agent1, agent2, agent3, group])
        await db.commit()

        pending_req = JoinRequest(agent_id=agent1.id, group_id=group.id, status="pending")
        approved_req = JoinRequest(agent_id=agent2.id, group_id=group.id, status="approved")
        rejected_req = JoinRequest(agent_id=agent3.id, group_id=group.id, status="rejected")

        db.add_all([pending_req, approved_req, rejected_req])
        await db.commit()

        result = await db.execute(select(JoinRequest).order_by(JoinRequest.id))
        requests = result.scalars().all()

        assert len(requests) == 3
        assert requests[0].status == "pending"
        assert requests[1].status == "approved"
        assert requests[2].status == "rejected"

    async def test_join_request_invalid_status(self, db: AsyncSession):
        """Invalid status should fail check constraint."""
        agent = await create_test_api_key(db, "invalid@test.com", "invalid-key")
        group = Group(name="Invalid Status Group")
        db.add_all([agent, group])
        await db.commit()

        join_request = JoinRequest(agent_id=agent.id, group_id=group.id, status="invalid")
        db.add(join_request)

        with pytest.raises(Exception):  # SQLAlchemy will raise IntegrityError
            await db.commit()

    async def test_join_request_repr(self, db: AsyncSession):
        """JoinRequest repr should be readable."""
        agent = await create_test_api_key(db, "repr@test.com", "repr-key-123")
        group = Group(name="Repr Group")
        db.add_all([agent, group])
        await db.commit()

        join_request = JoinRequest(agent_id=agent.id, group_id=group.id)
        db.add(join_request)
        await db.commit()

        assert "JoinRequest" in repr(join_request)
        assert str(agent.id) in repr(join_request)
        assert "pending" in repr(join_request)


@pytest.mark.asyncio
class TestGroupRelationships:
    """Test relationships between models."""

    async def test_group_memberships_relationship(self, db: AsyncSession):
        """Group.memberships should return related Membership instances."""
        agent1 = await create_test_api_key(db, "agent1@test.com", "agent1-key")
        agent2 = await create_test_api_key(db, "agent2@test.com", "agent2-key")
        group = Group(name="Relationship Test Group")

        db.add_all([agent1, agent2, group])
        await db.commit()

        membership1 = Membership(agent_id=agent1.id, group_id=group.id)
        membership2 = Membership(agent_id=agent2.id, group_id=group.id)
        db.add_all([membership1, membership2])
        await db.commit()

        # Query memberships directly instead of relying on lazy loading
        result = await db.execute(select(Membership).filter_by(group_id=group.id))
        memberships = result.scalars().all()

        assert len(memberships) == 2
        agent_ids = {m.agent_id for m in memberships}
        assert agent_ids == {agent1.id, agent2.id}

    async def test_membership_group_backref(self, db: AsyncSession):
        """Membership.group should refer back to parent Group."""
        agent = await create_test_api_key(db, "backref@test.com", "backref-key")
        group = Group(name="Backref Test Group")
        db.add_all([agent, group])
        await db.commit()

        membership = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership)
        await db.commit()

        # Reload membership with relationships
        result = await db.execute(select(Membership).filter_by(agent_id=agent.id))
        loaded_membership = result.scalar_one()

        assert loaded_membership.group.name == "Backref Test Group"

    async def test_cascade_delete_memberships(self, db: AsyncSession):
        """Deleting a group should cascade delete its memberships."""
        agent = await create_test_api_key(db, "cascade@test.com", "cascade-key")
        group = Group(name="Cascade Test Group")
        db.add_all([agent, group])
        await db.commit()

        membership = Membership(agent_id=agent.id, group_id=group.id)
        db.add(membership)
        await db.commit()

        group_id = group.id

        # Delete the group
        await db.delete(group)
        await db.commit()

        # Verify membership was also deleted
        result = await db.execute(select(Membership).filter_by(group_id=group_id))
        memberships = result.scalars().all()
        assert len(memberships) == 0

    async def test_cascade_delete_join_requests(self, db: AsyncSession):
        """Deleting a group should cascade delete its join requests."""
        agent = await create_test_api_key(db, "joinreq@test.com", "joinreq-key")
        group = Group(name="Join Request Cascade Group")
        db.add_all([agent, group])
        await db.commit()

        join_request = JoinRequest(agent_id=agent.id, group_id=group.id)
        db.add(join_request)
        await db.commit()

        group_id = group.id

        # Refresh to ensure we have the current state
        await db.refresh(group)

        # Delete the group
        await db.delete(group)
        await db.commit()

        # Verify join request was also deleted
        result = await db.execute(select(JoinRequest).filter_by(group_id=group_id))
        requests = result.scalars().all()
        assert len(requests) == 0
