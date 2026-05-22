"""Tests for channel-scoped messaging routes."""

import pytest
from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


async def _create_group_and_channel(client: AsyncClient) -> tuple[int, int]:
    """Helper: create a group and use its default #general channel."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Msg Group", "description": "For message tests"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    resp = await client.get(
        f"/api/groups/{group_id}/channels",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    channel_id = resp.json()[0]["id"]

    return group_id, channel_id


@pytest.mark.asyncio
async def test_post_message_to_channel(client: AsyncClient):
    """Post a message to a channel returns 201 with TLDR summary."""
    _, channel_id = await _create_group_and_channel(client)

    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Hello world", "body_markdown": "This is a test message for the channel."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["subject"] == "Hello world"
    assert data["author"] == "alice@herd.ai"
    assert "tldr" in data
    assert data["token_cost"] > 0
    assert data["parent_id"] is None


@pytest.mark.asyncio
async def test_post_message_as_non_member(client: AsyncClient):
    """Post message as non-member returns 403."""
    _, channel_id = await _create_group_and_channel(client)

    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Intruder", "body_markdown": "Should not be allowed."},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_channel_messages(client: AsyncClient):
    """List messages in a channel returns TLDRs only (no body_markdown)."""
    _, channel_id = await _create_group_and_channel(client)

    # Post two messages
    await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "First", "body_markdown": "First message body content."},
        headers=ALICE_HEADERS,
    )
    await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Second", "body_markdown": "Second message body content."},
        headers=ALICE_HEADERS,
    )

    resp = await client.get(
        f"/api/channels/{channel_id}/messages",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 2
    # Verify no body_markdown in list response
    for msg in messages:
        assert "body_markdown" not in msg
        assert "tldr" in msg
        assert "author" in msg


@pytest.mark.asyncio
async def test_list_channel_messages_since_filter(client: AsyncClient):
    """List messages with ?since= filter only returns newer messages."""
    _, channel_id = await _create_group_and_channel(client)

    # Post a message
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Old", "body_markdown": "This is the older message."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    first_ts = resp.json()["timestamp"]

    # Post another message
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "New", "body_markdown": "This is the newer message."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201

    # Filter since the first message timestamp — should only get the second
    resp = await client.get(
        f"/api/channels/{channel_id}/messages",
        params={"since": first_ts},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 1
    assert messages[0]["subject"] == "New"


@pytest.mark.asyncio
async def test_get_full_message(client: AsyncClient):
    """Get full message returns body_markdown."""
    _, channel_id = await _create_group_and_channel(client)

    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Full body", "body_markdown": "The complete message content here."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    resp = await client.get(f"/api/messages/{msg_id}", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["body_markdown"] == "The complete message content here."
    assert data["channel_id"] == channel_id
    assert data["subject"] == "Full body"


@pytest.mark.asyncio
async def test_get_nonexistent_message(client: AsyncClient):
    """Get non-existent message returns 404."""
    resp = await client.get("/api/messages/9999", headers=ALICE_HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_threaded_reply(client: AsyncClient):
    """Post a threaded reply with parent_id returns parent_id in response."""
    _, channel_id = await _create_group_and_channel(client)

    # Post parent message
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Parent", "body_markdown": "This is the parent message."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    parent_id = resp.json()["id"]

    # Post reply
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={
            "subject": "Reply",
            "body_markdown": "This is a reply to the parent.",
            "parent_id": parent_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["parent_id"] == parent_id

    # Verify in list
    resp = await client.get(
        f"/api/channels/{channel_id}/messages",
        headers=ALICE_HEADERS,
    )
    messages = resp.json()
    reply = next(m for m in messages if m["subject"] == "Reply")
    assert reply["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_channel_not_found(client: AsyncClient):
    """Post to non-existent channel returns 404."""
    resp = await client.post(
        "/api/channels/9999/messages",
        json={"subject": "No channel", "body_markdown": "Where does this go?"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_messages_non_member(client: AsyncClient):
    """List messages as non-member returns 403."""
    _, channel_id = await _create_group_and_channel(client)

    resp = await client.get(
        f"/api/channels/{channel_id}/messages",
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403
