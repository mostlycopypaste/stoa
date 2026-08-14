"""Tests for thread view API (issue #15)."""

import pytest

HEADERS = {"X-API-Key": "alice-key"}


async def _create_post(client, subject="Thread test", body="Body text"):
    resp = await client.post(
        "/api/posts",
        headers=HEADERS,
        json={"subject": subject, "body_markdown": body},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_comment(client, post_id, body, in_reply_to=None):
    payload = {"body_markdown": body}
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to
    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        headers=HEADERS,
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_thread_returns_threaded_structure(client):
    """GET /api/posts/{id}/thread returns threaded structure."""
    post_id = await _create_post(client)
    c1 = await _create_comment(client, post_id, "Top-level 1")
    c2 = await _create_comment(client, post_id, "Reply to 1", in_reply_to=c1)
    c3 = await _create_comment(client, post_id, "Reply to 2", in_reply_to=c2)

    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["post"]["id"] == post_id
    assert len(data["comments"]) == 1
    top = data["comments"][0]
    assert top["id"] == c1
    assert top["body_markdown"] == "Top-level 1"
    assert len(top["replies"]) == 1
    assert top["replies"][0]["id"] == c2
    assert top["replies"][0]["in_reply_to"] == c1
    assert len(top["replies"][0]["replies"]) == 1
    assert top["replies"][0]["replies"][0]["id"] == c3


@pytest.mark.asyncio
async def test_thread_nested_replies_correct_parent(client):
    """Nested replies appear under correct parent."""
    post_id = await _create_post(client)
    c1 = await _create_comment(client, post_id, "First")
    c2 = await _create_comment(client, post_id, "Second")
    c3 = await _create_comment(client, post_id, "Reply to first", in_reply_to=c1)
    c4 = await _create_comment(client, post_id, "Reply to second", in_reply_to=c2)

    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["comments"]) == 2
    assert data["comments"][0]["id"] == c1
    assert data["comments"][1]["id"] == c2
    assert len(data["comments"][0]["replies"]) == 1
    assert data["comments"][0]["replies"][0]["id"] == c3
    assert len(data["comments"][1]["replies"]) == 1
    assert data["comments"][1]["replies"][0]["id"] == c4


@pytest.mark.asyncio
async def test_thread_deeply_nested_3_levels(client):
    """Test deeply nested replies (3+ levels)."""
    post_id = await _create_post(client)
    c1 = await _create_comment(client, post_id, "L0")
    c2 = await _create_comment(client, post_id, "L1", in_reply_to=c1)
    c3 = await _create_comment(client, post_id, "L2", in_reply_to=c2)
    c4 = await _create_comment(client, post_id, "L3", in_reply_to=c3)

    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    node = data["comments"][0]
    for expected_id in [c1, c2, c3, c4]:
        assert node["id"] == expected_id
        if node["id"] != c4:
            assert len(node["replies"]) == 1
            node = node["replies"][0]
        else:
            assert len(node["replies"]) == 0


@pytest.mark.asyncio
async def test_thread_orphaned_comment_treated_as_top_level(client):
    """Orphaned comment (reply to deleted comment) treated as top-level."""
    post_id = await _create_post(client)
    c1 = await _create_comment(client, post_id, "Parent")
    c2 = await _create_comment(client, post_id, "Child", in_reply_to=c1)

    # Delete the parent comment (alice owns both).
    del_resp = await client.delete(
        f"/api/posts/{post_id}/comments/{c1}", headers=HEADERS
    )
    assert del_resp.status_code == 204

    # c2 is now orphaned — its in_reply_to points to a deleted comment.
    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    # c2 should appear as top-level since its parent was deleted.
    top_ids = [c["id"] for c in data["comments"]]
    assert c2 in top_ids
    # c1 should not appear (deleted).
    assert c1 not in top_ids


@pytest.mark.asyncio
async def test_thread_nonexistent_post_404(client):
    """Thread on non-existent post returns 404."""
    resp = await client.get("/api/posts/99999/thread", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_thread_channel_scoped_requires_membership(client):
    """Thread on channel-scoped post requires membership (403 for non-members)."""
    # Create a channel-scoped post (alice is in the group by default in test setup).
    # First create a group and channel.
    group_resp = await client.post(
        "/api/groups",
        headers=HEADERS,
        json={"name": "Thread Test Group", "visibility": "private"},
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]

    chan_resp = await client.post(
        f"/api/groups/{group_id}/channels",
        headers=HEADERS,
        json={"name": "Thread Channel", "topic": "threading"},
    )
    assert chan_resp.status_code == 201, chan_resp.text
    channel_id = chan_resp.json()["id"]

    # Create post in the channel (alice is a member).
    post_resp = await client.post(
        "/api/posts",
        headers=HEADERS,
        json={"subject": "Channel post", "body_markdown": "body", "channel_id": channel_id},
    )
    assert post_resp.status_code == 201, post_resp.text
    post_id = post_resp.json()["id"]

    # Bob is NOT a member of this private group — should get 403.
    resp = await client.get(
        f"/api/posts/{post_id}/thread",
        headers={"X-API-Key": "bob-key"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_thread_preserves_timestamp_ordering(client):
    """Comment tree preserves timestamp ordering within each level."""
    post_id = await _create_post(client)
    c1 = await _create_comment(client, post_id, "First top-level")
    c2 = await _create_comment(client, post_id, "Second top-level")
    c3 = await _create_comment(client, post_id, "Reply to first", in_reply_to=c1)
    c4 = await _create_comment(client, post_id, "Another reply to first", in_reply_to=c1)

    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    # Top-level should be ordered by timestamp ASC.
    assert data["comments"][0]["id"] == c1
    assert data["comments"][1]["id"] == c2

    # Replies under c1 should be ordered by timestamp ASC.
    replies = data["comments"][0]["replies"]
    assert replies[0]["id"] == c3
    assert replies[1]["id"] == c4


@pytest.mark.asyncio
async def test_thread_empty_comments(client):
    """Empty thread (post with no comments) returns empty comments array."""
    post_id = await _create_post(client)

    resp = await client.get(f"/api/posts/{post_id}/thread", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["post"]["id"] == post_id
    assert data["comments"] == []
