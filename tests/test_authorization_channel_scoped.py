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


# ---------------------------------------------------------------------------
# Issue #49 — parent_post_id / parent_id validation on post creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_cross_tenant_parent_post_id_rejected(client: AsyncClient):
    """POST /api/posts with parent_post_id in a private channel Bob can't access → 403."""
    _, channel_id = await _alice_group_channel(client)

    # Alice creates a post in her private channel
    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Alice private post",
            "body_markdown": "Private content.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    parent_id = resp.json()["id"]

    # Bob tries to create a post referencing Alice's private post as parent
    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Bob reply",
            "body_markdown": "Trying to inject a reply edge.",
            "parent_post_id": parent_id,
        },
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_nonexistent_parent_post_id_returns_404(client: AsyncClient):
    """POST /api/posts with a nonexistent parent_post_id → 404, not 500."""
    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Orphan reply",
            "body_markdown": "Points to a nonexistent parent.",
            "parent_post_id": 999999,
        },
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_message_cross_channel_parent_id_rejected(client: AsyncClient):
    """POST /api/channels/{id}/messages with parent_id in a different channel → 400."""
    _, alice_channel_id = await _alice_group_channel(client)

    # Alice creates a post in her channel
    resp = await client.post(
        f"/api/channels/{alice_channel_id}/messages",
        json={"subject": "Alice msg", "body_markdown": "In Alice's channel."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    parent_id = resp.json()["id"]

    # Bob creates his own group + channel
    resp = await client.post(
        "/api/groups",
        json={"name": "Bob Group", "description": "Bob's group"},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 201
    bob_group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{bob_group_id}/channels", headers=BOB_HEADERS)
    assert resp.status_code == 200
    bob_channel_id = resp.json()[0]["id"]

    # Bob tries to link his message to Alice's parent in a different channel
    resp = await client.post(
        f"/api/channels/{bob_channel_id}/messages",
        json={
            "subject": "Bob cross-channel reply",
            "body_markdown": "Trying to link to Alice's channel.",
            "parent_id": parent_id,
        },
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_message_same_channel_parent_id_accepted(client: AsyncClient):
    """POST /api/channels/{id}/messages with parent_id in the same channel → 201 (regression)."""
    _, channel_id = await _alice_group_channel(client)

    # Alice creates a parent message
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Parent msg", "body_markdown": "First message."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    parent_id = resp.json()["id"]

    # Alice creates a reply referencing the parent in the same channel
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={
            "subject": "Reply msg",
            "body_markdown": "Reply to parent.",
            "parent_id": parent_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["parent_id"] == parent_id
