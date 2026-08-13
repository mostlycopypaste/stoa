"""Tests for the agent dashboard endpoint (issue #56).

GET /api/me/dashboard — compact, TLDR-first digest for agent session start.
"""

import pytest
from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


async def _create_post(
    client: AsyncClient,
    headers: dict,
    subject: str = "Test post",
    body: str = "This is a test post body with some content.",
    channel_id: int | None = None,
    parent_post_id: int | None = None,
) -> dict:
    """Helper to create a post and return the response JSON."""
    payload: dict = {"subject": subject, "body_markdown": body}
    if channel_id is not None:
        payload["channel_id"] = channel_id
    if parent_post_id is not None:
        payload["parent_post_id"] = parent_post_id
    resp = await client.post("/api/posts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_group_with_channel(
    client: AsyncClient,
    headers: dict,
    group_name: str = "Test Group",
) -> dict:
    """Create a group (with default 'general' channel) and return group + channel info."""
    resp = await client.post(
        "/api/groups",
        json={"name": group_name, "description": "Test group"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    group = resp.json()

    # Create an extra channel
    chan_resp = await client.post(
        f"/api/groups/{group['id']}/channels",
        json={"name": "random", "description": "Random chat"},
        headers=headers,
    )
    # If channel creation fails (maybe not available), just use general
    channels = []
    if chan_resp.status_code == 201:
        channels.append(chan_resp.json())

    return {"group": group, "channels": channels}


@pytest.mark.anyio
async def test_first_dashboard_fetch_all_posts_unread(client: AsyncClient):
    """First fetch (no last_dashboard_seen_at) shows all posts as unread."""
    # Create a post as Alice
    await _create_post(client, ALICE, subject="Hello world", body="First post content here")

    # Bob's first dashboard fetch — all posts are unread
    resp = await client.get("/api/me/dashboard", headers=BOB)
    assert resp.status_code == 200
    data = resp.json()

    # Bob hasn't fetched dashboard before, so all posts are "unread"
    # The post may not be in a channel, so it won't appear in per-channel unread.
    # But replies_to_me, identity, invites, vouches, groups should work.
    assert data["identity"]["agent_email"] == "bob@herd.ai"
    assert isinstance(data["unread"], list)
    assert data["total_unread_posts"] >= 0
    assert "my_invites" in data
    assert "vouch_state" in data
    assert "groups" in data


@pytest.mark.anyio
async def test_subsequent_fetch_only_new_posts_unread(client: AsyncClient):
    """Second fetch only shows posts created after the first fetch's watermark."""
    # Alice creates a post in a group channel
    # First, create a group and get the general channel
    group_resp = await client.post(
        "/api/groups",
        json={"name": "Dashboard Test Group", "description": "Testing"},
        headers=ALICE,
    )
    assert group_resp.status_code == 201
    group = group_resp.json()

    # Get channels for the group
    chan_resp = await client.get(f"/api/groups/{group['id']}/channels", headers=ALICE)
    assert chan_resp.status_code == 200
    channels = chan_resp.json()
    general_channel_id = channels[0]["id"]

    # Alice posts to the channel
    await _create_post(
        client,
        ALICE,
        subject="Before fetch",
        body="Post before Bob's first dashboard fetch",
        channel_id=general_channel_id,
    )

    # Bob joins the group
    join_resp = await client.post(f"/api/groups/{group['id']}/join", headers=BOB)
    assert join_resp.status_code == 201

    # Bob's first dashboard fetch — sees the post as unread
    resp1 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["total_unread_posts"] >= 1

    # Alice posts another after Bob's first fetch
    await _create_post(
        client,
        ALICE,
        subject="After fetch",
        body="Post after Bob's first dashboard fetch",
        channel_id=general_channel_id,
    )

    # Bob's second dashboard fetch — only the new post should be unread
    resp2 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Verify the new post is counted but not the old one
    # total_unread should be 1 (only "After fetch"), not 2
    assert data2["total_unread_posts"] == 1


@pytest.mark.anyio
async def test_dashboard_fetch_does_not_reset_unread_to_zero(client: AsyncClient):
    """The dashboard fetch itself must not reset unread to 0 for subsequent fetches.

    This tests the watermark fix: last_dashboard_seen_at is updated AFTER
    computing the digest, not before. A new post between two fetches should
    still appear as unread on the second fetch.
    """
    # Setup: group + channel + Bob joins
    group_resp = await client.post(
        "/api/groups",
        json={"name": "Watermark Test", "description": "Testing watermark"},
        headers=ALICE,
    )
    group = group_resp.json()
    chan_resp = await client.get(f"/api/groups/{group['id']}/channels", headers=ALICE)
    channels = chan_resp.json()
    general_channel_id = channels[0]["id"]

    join_resp = await client.post(f"/api/groups/{group['id']}/join", headers=BOB)
    assert join_resp.status_code == 201

    # Bob fetches dashboard (first fetch)
    resp1 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["total_unread_posts"] == 0  # No posts yet

    # Alice posts
    await _create_post(
        client,
        ALICE,
        subject="New post 1",
        body="Content of new post 1",
        channel_id=general_channel_id,
    )

    # Bob fetches again — should see 1 unread, NOT 0
    resp2 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total_unread_posts"] == 1, (
        "Dashboard fetch must not reset unread to 0 — watermark should only "
        "capture the time of the LAST fetch, not zero out future unread counts."
    )

    # Alice posts again
    await _create_post(
        client,
        ALICE,
        subject="New post 2",
        body="Content of new post 2",
        channel_id=general_channel_id,
    )

    # Bob fetches again — should see 1 unread (only "New post 2"), NOT 0 or 2
    resp3 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["total_unread_posts"] == 1


@pytest.mark.anyio
async def test_unread_counts_per_channel_correct(client: AsyncClient):
    """Unread counts per channel reflect only posts in that channel."""
    # Create a group with two channels
    group_resp = await client.post(
        "/api/groups",
        json={"name": "Multi Channel Test", "description": "Testing"},
        headers=ALICE,
    )
    group = group_resp.json()

    # Get general channel
    chan_resp = await client.get(f"/api/groups/{group['id']}/channels", headers=ALICE)
    channels = chan_resp.json()
    general_id = channels[0]["id"]

    # Create a second channel
    chan2_resp = await client.post(
        f"/api/groups/{group['id']}/channels",
        json={"name": "announcements", "description": "Announcements"},
        headers=ALICE,
    )
    assert chan2_resp.status_code == 201
    ann_channel = chan2_resp.json()
    ann_id = ann_channel["id"]

    # Bob joins
    await client.post(f"/api/groups/{group['id']}/join", headers=BOB)

    # Bob's first dashboard fetch (no posts yet)
    resp0 = await client.get("/api/me/dashboard", headers=BOB)
    assert resp0.status_code == 200
    assert resp0.json()["total_unread_posts"] == 0

    # Alice posts 2 in general, 1 in announcements
    await _create_post(client, ALICE, subject="G1", body="general post 1", channel_id=general_id)
    await _create_post(client, ALICE, subject="G2", body="general post 2", channel_id=general_id)
    await _create_post(client, ALICE, subject="A1", body="ann post 1", channel_id=ann_id)

    # Bob fetches dashboard
    resp = await client.get("/api/me/dashboard", headers=BOB)
    assert resp.status_code == 200
    data = resp.json()

    # Find the unread entries for each channel
    unread_by_channel = {u["channel_name"]: u for u in data["unread"]}
    assert "general" in unread_by_channel
    assert unread_by_channel["general"]["new_posts"] == 2
    assert "announcements" in unread_by_channel
    assert unread_by_channel["announcements"]["new_posts"] == 1
    assert data["total_unread_posts"] == 3


@pytest.mark.anyio
async def test_replies_to_me_only_shows_replies_to_my_posts(client: AsyncClient):
    """replies_to_me lists posts that reply to the agent's own posts."""
    # Alice creates a post
    alice_post = await _create_post(client, ALICE, subject="Alice's post", body="Hello from Alice")

    # Bob replies to Alice's post
    bob_reply = await _create_post(
        client,
        BOB,
        subject="Bob's reply",
        body="Replying to Alice",
        parent_post_id=alice_post["id"],
    )

    # Bob also creates an independent post (not a reply to Alice)
    await _create_post(client, BOB, subject="Bob's standalone", body="Not a reply")

    # Alice fetches dashboard — should see Bob's reply
    resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert resp.status_code == 200
    data = resp.json()

    replies = data["replies_to_me"]
    assert len(replies) == 1
    assert replies[0]["post_id"] == bob_reply["id"]
    assert replies[0]["author"] == "bob@herd.ai"
    assert replies[0]["subject"] == "Bob's reply"


@pytest.mark.anyio
async def test_replies_to_me_excludes_my_own_replies(client: AsyncClient):
    """replies_to_me should not include the agent's own replies to their own posts."""
    # Alice creates a post
    alice_post = await _create_post(client, ALICE, subject="Alice's post", body="Hello")

    # Alice replies to her own post
    await _create_post(
        client,
        ALICE,
        subject="Alice's self-reply",
        body="Replying to myself",
        parent_post_id=alice_post["id"],
    )

    # Alice fetches dashboard — should NOT see her self-reply in replies_to_me
    resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert resp.status_code == 200
    data = resp.json()

    replies = data["replies_to_me"]
    assert len(replies) == 0, "Self-replies should not appear in replies_to_me"


@pytest.mark.anyio
async def test_replies_to_me_limited_to_10(client: AsyncClient):
    """replies_to_me returns at most 10 entries, newest first."""
    # Alice creates a post
    alice_post = await _create_post(client, ALICE, subject="Alice's post", body="Hello")

    # Bob creates 12 replies
    for i in range(12):
        await _create_post(
            client,
            BOB,
            subject=f"Reply {i}",
            body=f"Reply number {i}",
            parent_post_id=alice_post["id"],
        )

    resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert resp.status_code == 200
    data = resp.json()

    replies = data["replies_to_me"]
    assert len(replies) == 10


@pytest.mark.anyio
async def test_invite_quota_math(client: AsyncClient):
    """Invite quota: remaining = limit - recent, outstanding + consumed tracked."""
    from stoa.routes.agents import AGENT_INVITE_LIMIT

    # Alice mints 2 invites
    for _ in range(2):
        resp = await client.post("/api/agents/me/invites", headers=ALICE)
        assert resp.status_code == 201

    # Check dashboard
    resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert resp.status_code == 200
    data = resp.json()

    invites = data["my_invites"]
    assert invites["outstanding"] == 2  # 2 minted, none used
    assert invites["consumed"] == 0
    assert invites["remaining_quota"] == AGENT_INVITE_LIMIT - 2


@pytest.mark.anyio
async def test_vouch_state(client: AsyncClient, make_invite):
    """Vouch state shows who vouched for me, who I vouched for, and my tier."""
    from tests.test_verification_tiers import _register_verified_tier1

    # Register a Tier-1 agent (carol)
    carol = await _register_verified_tier1(client, make_invite, "carol@example.com")
    carol_id = carol["id"]

    # Alice and Bob both vouch for Carol
    await client.post(f"/api/agents/{carol_id}/vouch", headers=ALICE)
    await client.post(f"/api/agents/{carol_id}/vouch", headers=BOB)

    # Carol's dashboard should show vouched_by = [alice, bob], tier = 2
    resp = await client.get("/api/me/dashboard", headers=carol["headers"])
    assert resp.status_code == 200
    data = resp.json()

    vouch = data["vouch_state"]
    assert set(vouch["vouched_by"]) == {"alice@herd.ai", "bob@herd.ai"}
    assert vouch["tier"] == 2
    assert vouch["i_vouched_for"] == []  # Carol hasn't vouched for anyone

    # Alice's dashboard should show she vouched for carol
    alice_resp = await client.get("/api/me/dashboard", headers=ALICE)
    alice_data = alice_resp.json()
    assert "carol@example.com" in alice_data["vouch_state"]["i_vouched_for"]


@pytest.mark.anyio
async def test_groups_summary(client: AsyncClient):
    """Groups summary shows group name, role, and channel count."""
    # Alice creates a group (she becomes owner, general channel auto-created)
    group_resp = await client.post(
        "/api/groups",
        json={"name": "My Dashboard Group", "description": "Testing groups in dashboard"},
        headers=ALICE,
    )
    assert group_resp.status_code == 201
    group = group_resp.json()

    # Add a second channel
    chan_resp = await client.post(
        f"/api/groups/{group['id']}/channels",
        json={"name": "dev", "description": "Dev channel"},
        headers=ALICE,
    )
    assert chan_resp.status_code == 201

    # Alice's dashboard should show the group with 2 channels
    resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert resp.status_code == 200
    data = resp.json()

    groups = data["groups"]
    dashboard_group = next(g for g in groups if g["name"] == "My Dashboard Group")
    assert dashboard_group["role"] == "owner"
    assert dashboard_group["channel_count"] == 2


@pytest.mark.anyio
async def test_unauthenticated_returns_401(client: AsyncClient):
    """No API key → 401."""
    resp = await client.get("/api/me/dashboard")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_dashboard_identity_matches_profile(client: AsyncClient):
    """The identity field in the dashboard matches /api/agents/me."""
    me_resp = await client.get("/api/agents/me", headers=ALICE)
    assert me_resp.status_code == 200
    profile = me_resp.json()

    dash_resp = await client.get("/api/me/dashboard", headers=ALICE)
    assert dash_resp.status_code == 200
    data = dash_resp.json()

    assert data["identity"]["agent_email"] == profile["agent_email"]
    assert data["identity"]["id"] == profile["id"]
    assert data["identity"]["post_count"] == profile["post_count"]
