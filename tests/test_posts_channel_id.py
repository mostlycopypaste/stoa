"""Tests for channel_id on POST /api/posts (added in PR #44).

Regression guard: the generic post-create endpoint must not become a
side door that bypasses the membership checks enforced by
POST /api/channels/{channel_id}/messages.
"""

import pytest
from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


async def _alice_group_channel(client: AsyncClient) -> tuple[int, int]:
    """Create a group owned by Alice and return (group_id, default channel_id)."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Channel ID Group", "description": "For channel_id tests"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    return group_id, resp.json()[0]["id"]


@pytest.mark.asyncio
async def test_member_can_create_post_in_channel(client: AsyncClient):
    """A group member can target a channel via channel_id."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "This post should land in the channel.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_non_member_cannot_create_post_in_channel(client: AsyncClient):
    """Non-members must be rejected, matching /api/channels/{id}/messages."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Intruder",
            "body_markdown": "Should not be allowed into someone else's channel.",
            "channel_id": channel_id,
        },
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_nonexistent_channel_is_rejected(client: AsyncClient):
    """A channel_id that does not exist must 404, not silently persist."""
    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Ghost channel",
            "body_markdown": "There is no channel with this id at all.",
            "channel_id": 999999,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_channel_id_omitted_still_works(client: AsyncClient):
    """Unscoped posts remain valid (backwards compatibility)."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Global post", "body_markdown": "No channel scoping here."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
