"""Authorization tests for channel-scoped reads and comments (issues #46, #47, #48).

Regression guard: the single-message read, single-post read, and comment
endpoints must enforce the same group-membership check as the write-side
endpoints fixed in PR #45.  Unscoped posts remain public.
"""

import pytest
from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


async def _alice_group_channel(client: AsyncClient) -> tuple[int, int]:
    """Create a group owned by Alice and return (group_id, default channel_id)."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Authz Group", "description": "For channel-scoped authz tests"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    return group_id, resp.json()[0]["id"]


# ---------------------------------------------------------------------------
# Issue #46 — GET /api/messages/{message_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_get_channel_message(client: AsyncClient):
    """A group member can read a channel-scoped message by id."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Secret", "body_markdown": "Private channel content."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    resp = await client.get(f"/api/messages/{msg_id}", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["body_markdown"] == "Private channel content."


@pytest.mark.asyncio
async def test_non_member_cannot_get_channel_message(client: AsyncClient):
    """A non-member must be denied (403) when reading a channel-scoped message."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Secret", "body_markdown": "Private channel content."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    resp = await client.get(f"/api/messages/{msg_id}", headers=BOB_HEADERS)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Issue #48 — GET /api/posts/{post_id} (channel-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_member_cannot_get_channel_scoped_post(client: AsyncClient):
    """A non-member must be denied (403) when reading a channel-scoped post."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "Private scoped post body.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_can_get_channel_scoped_post(client: AsyncClient):
    """A group member can read a channel-scoped post by id."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "Private scoped post body.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["body_markdown"] == "Private scoped post body."


@pytest.mark.asyncio
async def test_unscoped_post_remains_public(client: AsyncClient):
    """Regression: unscoped posts (no channel_id) must remain publicly readable."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Public post", "body_markdown": "Everyone can read this."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["body_markdown"] == "Everyone can read this."


# ---------------------------------------------------------------------------
# Issue #47 — POST /api/posts/{post_id}/comments (channel-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_member_cannot_comment_on_channel_scoped_post(client: AsyncClient):
    """A non-member must be denied (403) when commenting on a channel-scoped post."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "Private scoped post body.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": "Intruder comment."},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_can_comment_on_channel_scoped_post(client: AsyncClient):
    """A group member can comment on a channel-scoped post."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "Private scoped post body.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": "Member comment."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["body_markdown"] == "Member comment."


@pytest.mark.asyncio
async def test_unscoped_post_comment_remains_public(client: AsyncClient):
    """Regression: commenting on unscoped posts remains open to all."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Public post", "body_markdown": "Everyone can read this."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": "Bob's public comment."},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 201
