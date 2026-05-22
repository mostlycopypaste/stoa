"""Tests for channel management routes."""

import pytest
from httpx import AsyncClient

from stoa.rate_limit import reset_limiter


@pytest.mark.asyncio
async def test_list_channels_as_member(client: AsyncClient):
    """List channels as a member returns 200."""
    # Create a group
    resp = await client.post(
        "/api/groups",
        json={"name": "Test Group", "description": "A group", "visibility": "public"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Create a channel
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "general", "description": "General discussion"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201

    # List channels
    resp = await client.get(
        f"/api/groups/{group_id}/channels",
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 200
    channels = resp.json()
    assert len(channels) == 1
    assert channels[0]["name"] == "general"
    assert channels[0]["description"] == "General discussion"


@pytest.mark.asyncio
async def test_list_channels_as_non_member(client: AsyncClient):
    """List channels as non-member returns 403."""
    # Alice creates a group
    resp = await client.post(
        "/api/groups",
        json={"name": "Private Group", "description": "Alice's group", "visibility": "private"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Bob tries to list channels
    resp = await client.get(
        f"/api/groups/{group_id}/channels",
        headers={"X-API-Key": "bob-key"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_channel_as_owner(client: AsyncClient):
    """Create channel as owner returns 201."""
    # Create a group
    resp = await client.post(
        "/api/groups",
        json={"name": "My Group", "description": "Test", "visibility": "public"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Create a channel
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "announcements", "description": "Important updates", "topic": "News"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    channel = resp.json()
    assert channel["name"] == "announcements"
    assert channel["description"] == "Important updates"
    assert channel["topic"] == "News"
    assert channel["group_id"] == group_id


@pytest.mark.asyncio
async def test_create_channel_as_member_not_admin(client: AsyncClient):
    """Create channel as regular member (not admin) returns 403."""
    # Alice creates a group
    resp = await client.post(
        "/api/groups",
        json={"name": "Group", "description": "Test", "visibility": "public"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Alice invites Bob (invite adds Bob as a member directly)
    resp = await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "bob@herd.ai"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201

    # Bob tries to create a channel (should fail since he's only a member, not admin)
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "test"},
        headers={"X-API-Key": "bob-key"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_channel_duplicate_name(client: AsyncClient):
    """Create channel with duplicate name returns 409."""
    # Create a group
    resp = await client.post(
        "/api/groups",
        json={"name": "Group", "description": "Test", "visibility": "public"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Create first channel
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "duplicate"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201

    # Try to create second channel with same name
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "duplicate"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_channel_in_nonexistent_group(client: AsyncClient):
    """Create channel in non-existent group returns 404."""
    resp = await client.post(
        "/api/groups/9999/channels",
        json={"name": "test"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_max_channels_limit(client: AsyncClient):
    """Creating channels beyond max limit returns 409."""
    # Create a group
    resp = await client.post(
        "/api/groups",
        json={"name": "Group", "description": "Test", "visibility": "public"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    # Create 50 channels (the limit), resetting rate limiter every 9 requests
    # (rate limit is 10 requests per window, and we used 1 for group creation)
    for i in range(50):
        if i > 0 and i % 9 == 0:
            reset_limiter()
        resp = await client.post(
            f"/api/groups/{group_id}/channels",
            json={"name": f"channel-{i}"},
            headers={"X-API-Key": "alice-key"},
        )
        assert resp.status_code == 201

    # Reset limiter one more time before final request
    reset_limiter()

    # Try to create the 51st channel
    resp = await client.post(
        f"/api/groups/{group_id}/channels",
        json={"name": "channel-51"},
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 409
    assert "Maximum 50 channels" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_channels_group_not_found(client: AsyncClient):
    """List channels for non-existent group returns 404."""
    resp = await client.get(
        "/api/groups/9999/channels",
        headers={"X-API-Key": "alice-key"},
    )
    assert resp.status_code == 404
