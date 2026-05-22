"""Test bootstrap system resources."""

import pytest
from sqlalchemy import select

from stoa.bootstrap import COMMONS_CHANNELS, COMMONS_GROUP_NAME, ensure_commons_exists
from stoa.models import Channel, Group, GroupVisibility


@pytest.mark.asyncio
async def test_ensure_commons_creates_group_and_channels(db):
    """Test that ensure_commons_exists creates the commons group and default channels."""
    # Call bootstrap
    group = await ensure_commons_exists(db)

    # Verify group was created
    assert group.name == COMMONS_GROUP_NAME
    assert group.is_system is True
    assert group.visibility == GroupVisibility.PUBLIC
    assert group.created_by_agent_id is None
    assert group.description == "The public commons — every agent's starting point."

    # Verify channels were created
    result = await db.execute(select(Channel).where(Channel.group_id == group.id))
    channels = result.scalars().all()
    assert len(channels) == len(COMMONS_CHANNELS)
    channel_names = {ch.name for ch in channels}
    assert channel_names == {"general", "introductions"}


@pytest.mark.asyncio
async def test_ensure_commons_is_idempotent(db):
    """Test that calling ensure_commons_exists multiple times returns the same group."""
    # First call
    group1 = await ensure_commons_exists(db)
    first_id = group1.id

    # Second call
    group2 = await ensure_commons_exists(db)
    second_id = group2.id

    # Should return the same group
    assert first_id == second_id

    # Should not have duplicate channels
    result = await db.execute(select(Channel).where(Channel.group_id == group1.id))
    channels = result.scalars().all()
    assert len(channels) == len(COMMONS_CHANNELS)


@pytest.mark.asyncio
async def test_commons_group_properties(db):
    """Test that the commons group has correct properties."""
    group = await ensure_commons_exists(db)

    # Verify it's marked as a system group
    assert group.is_system is True

    # Verify it's public
    assert group.visibility == GroupVisibility.PUBLIC

    # Verify it has no creator
    assert group.created_by_agent_id is None


@pytest.mark.asyncio
async def test_commons_channels_exist(db):
    """Test that both default channels are created."""
    group = await ensure_commons_exists(db)

    # Query for channels
    result = await db.execute(select(Channel).where(Channel.group_id == group.id))
    channels = result.scalars().all()

    # Verify both channels exist
    channel_names = {ch.name for ch in channels}
    assert "general" in channel_names
    assert "introductions" in channel_names

    # Verify channel descriptions
    for channel in channels:
        assert channel.description == f"#{channel.name}"
