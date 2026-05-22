"""Tests for Channel model and channel_id on Post."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Channel, Group, Post

from .helpers import create_test_api_key


@pytest.mark.asyncio
class TestChannelModel:
    """Test Channel model."""

    async def test_create_channel_in_group(self, db: AsyncSession):
        """Create a channel within a group."""
        agent = await create_test_api_key(db, "creator@test.com", "test-key-123")
        group = Group(name="Test Group", created_by_agent_id=agent.id)
        db.add_all([agent, group])
        await db.commit()

        channel = Channel(
            name="general",
            description="General discussion",
            topic="Welcome to the group",
            group_id=group.id,
        )
        db.add(channel)
        await db.commit()

        result = await db.execute(select(Channel).filter_by(name="general"))
        saved_channel = result.scalar_one()

        assert saved_channel.name == "general"
        assert saved_channel.description == "General discussion"
        assert saved_channel.topic == "Welcome to the group"
        assert saved_channel.group_id == group.id

    async def test_channel_default_values(self, db: AsyncSession):
        """Channel description and topic should default to empty strings."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="minimal", group_id=group.id)
        db.add(channel)
        await db.commit()

        result = await db.execute(select(Channel).filter_by(name="minimal"))
        saved_channel = result.scalar_one()

        assert saved_channel.description == ""
        assert saved_channel.topic == ""

    async def test_channel_unique_name_per_group(self, db: AsyncSession):
        """Channel names must be unique within a group."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel1 = Channel(name="general", group_id=group.id)
        db.add(channel1)
        await db.commit()

        # Try to add another channel with the same name in the same group
        channel2 = Channel(name="general", group_id=group.id)
        db.add(channel2)

        with pytest.raises(Exception):  # SQLAlchemy will raise IntegrityError
            await db.commit()

    async def test_channel_same_name_different_groups(self, db: AsyncSession):
        """Same channel name can exist in different groups."""
        group1 = Group(name="Group 1")
        group2 = Group(name="Group 2")
        db.add_all([group1, group2])
        await db.commit()

        channel1 = Channel(name="general", group_id=group1.id)
        channel2 = Channel(name="general", group_id=group2.id)
        db.add_all([channel1, channel2])
        await db.commit()

        result = await db.execute(select(Channel).filter_by(name="general"))
        channels = result.scalars().all()

        assert len(channels) == 2
        group_ids = {c.group_id for c in channels}
        assert group_ids == {group1.id, group2.id}

    async def test_channel_repr(self, db: AsyncSession):
        """Channel repr should be readable."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="test-channel", group_id=group.id)
        db.add(channel)
        await db.commit()

        assert "Channel" in repr(channel)
        assert "test-channel" in repr(channel)
        assert str(group.id) in repr(channel)


@pytest.mark.asyncio
class TestChannelRelationships:
    """Test Channel relationships with Group."""

    async def test_channel_group_relationship(self, db: AsyncSession):
        """Channel.group should refer back to parent Group."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="general", group_id=group.id)
        db.add(channel)
        await db.commit()

        # Reload channel with relationships
        result = await db.execute(select(Channel).filter_by(name="general"))
        loaded_channel = result.scalar_one()

        assert loaded_channel.group.name == "Test Group"

    async def test_group_channels_relationship(self, db: AsyncSession):
        """Group.channels should return related Channel instances."""
        group = Group(name="Multi-Channel Group")
        db.add(group)
        await db.commit()

        channel1 = Channel(name="general", group_id=group.id)
        channel2 = Channel(name="random", group_id=group.id)
        channel3 = Channel(name="announcements", group_id=group.id)
        db.add_all([channel1, channel2, channel3])
        await db.commit()

        # Query channels directly
        result = await db.execute(select(Channel).filter_by(group_id=group.id).order_by(Channel.name))
        channels = result.scalars().all()

        assert len(channels) == 3
        channel_names = {c.name for c in channels}
        assert channel_names == {"general", "random", "announcements"}

    async def test_cascade_delete_channels_when_group_deleted(self, db: AsyncSession):
        """Deleting a group should cascade delete its channels."""
        group = Group(name="Temporary Group")
        db.add(group)
        await db.commit()

        channel1 = Channel(name="channel1", group_id=group.id)
        channel2 = Channel(name="channel2", group_id=group.id)
        db.add_all([channel1, channel2])
        await db.commit()

        group_id = group.id

        # Delete the group
        await db.delete(group)
        await db.commit()

        # Verify channels were also deleted
        result = await db.execute(select(Channel).filter_by(group_id=group_id))
        channels = result.scalars().all()
        assert len(channels) == 0


@pytest.mark.asyncio
class TestPostChannelId:
    """Test channel_id field on Post model."""

    async def test_post_channel_id_nullable(self, db: AsyncSession):
        """Post.channel_id should be nullable (existing posts have no channel)."""
        post = Post(
            message_id="test-msg-001",
            author="agent@example.com",
            subject="Test Subject",
            tldr="Test summary",
            body_markdown="Test body",
            body_html="<p>Test body</p>",
        )
        db.add(post)
        await db.commit()

        result = await db.execute(select(Post).filter_by(message_id="test-msg-001"))
        saved_post = result.scalar_one()

        assert saved_post.channel_id is None

    async def test_post_with_channel_id(self, db: AsyncSession):
        """Post can reference a channel."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="general", group_id=group.id)
        db.add(channel)
        await db.commit()

        post = Post(
            message_id="test-msg-002",
            author="agent@example.com",
            subject="Test Subject",
            tldr="Test summary",
            body_markdown="Test body",
            body_html="<p>Test body</p>",
            channel_id=channel.id,
        )
        db.add(post)
        await db.commit()

        result = await db.execute(select(Post).filter_by(message_id="test-msg-002"))
        saved_post = result.scalar_one()

        assert saved_post.channel_id == channel.id

    async def test_post_channel_id_set_null_on_channel_delete(self, db: AsyncSession):
        """Deleting a channel should SET NULL on Post.channel_id."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="temporary", group_id=group.id)
        db.add(channel)
        await db.commit()

        post = Post(
            message_id="test-msg-003",
            author="agent@example.com",
            subject="Test Subject",
            tldr="Test summary",
            body_markdown="Test body",
            body_html="<p>Test body</p>",
            channel_id=channel.id,
        )
        db.add(post)
        await db.commit()

        post_id = post.id
        channel_id = channel.id

        # Verify post has channel_id set
        result = await db.execute(select(Post).filter_by(id=post_id))
        pre_delete_post = result.scalar_one()
        assert pre_delete_post.channel_id == channel_id

        # Delete the channel
        await db.delete(channel)
        await db.commit()

        # Expire all to force reload from DB
        db.expire_all()

        # Verify post still exists but channel_id is NULL
        result = await db.execute(select(Post).filter_by(id=post_id))
        post_after_delete = result.scalar_one()
        assert post_after_delete.channel_id is None

    async def test_multiple_posts_same_channel(self, db: AsyncSession):
        """Multiple posts can reference the same channel."""
        group = Group(name="Test Group")
        db.add(group)
        await db.commit()

        channel = Channel(name="general", group_id=group.id)
        db.add(channel)
        await db.commit()

        post1 = Post(
            message_id="test-msg-004",
            author="agent1@example.com",
            subject="Post 1",
            tldr="Summary 1",
            body_markdown="Body 1",
            body_html="<p>Body 1</p>",
            channel_id=channel.id,
        )
        post2 = Post(
            message_id="test-msg-005",
            author="agent2@example.com",
            subject="Post 2",
            tldr="Summary 2",
            body_markdown="Body 2",
            body_html="<p>Body 2</p>",
            channel_id=channel.id,
        )
        db.add_all([post1, post2])
        await db.commit()

        result = await db.execute(select(Post).filter_by(channel_id=channel.id))
        posts = result.scalars().all()

        assert len(posts) == 2
        assert all(p.channel_id == channel.id for p in posts)
