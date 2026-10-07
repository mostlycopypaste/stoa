"""Tests for channel_id on POST /api/posts (added in PR #44, require-or-inherit per #168).

Regression guard on two fronts:
1. The generic post-create endpoint must not become a side door that
   bypasses the membership checks enforced by
   POST /api/channels/{channel_id}/messages.
2. Every post must land in a channel (issue #168): standalone posts carry
   an explicit ``channel_id``; replies (``parent_post_id``) inherit the
   parent's channel when ``channel_id`` is omitted. Channel-less "orphan"
   posts are invisible in every channel listing yet readable by id
   (fail-open), so creation must fail closed with a 400 instead.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import Post

from .conftest import TestSession

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


async def _second_channel(client: AsyncClient) -> int:
    """A second group's default channel owned by Alice (for mismatch tests)."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Second Channel Group", "description": "Mismatch target"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]
    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE_HEADERS)
    return resp.json()[0]["id"]


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
async def test_standalone_post_without_channel_id_is_rejected(client: AsyncClient):
    """Issue #168: channel-less posts are invisible in listings yet readable
    by id — creation must fail closed with a clear 400, not 201 an orphan."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Global post", "body_markdown": "No channel scoping here."},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 400
    assert "channel_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_present_channel_id_persists_echoes_and_enumerates(client: AsyncClient):
    """The post-51 sighting: a client that sends channel_id must get it back
    honestly (create echo + detail GET), see the row persisted, and find the
    post in the channel listing and dashboard unread surface."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Scoped post",
            "body_markdown": "Cheng Yi's post-51 shape: explicit channel_id.",
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["channel_id"] == channel_id
    post_id = data["id"]

    async with TestSession() as session:
        persisted = (
            await session.execute(select(Post.channel_id).where(Post.id == post_id))
        ).scalar_one()
    assert persisted == channel_id

    listing = await client.get(f"/api/channels/{channel_id}/messages", headers=ALICE_HEADERS)
    assert post_id in [p["id"] for p in listing.json()]

    dashboard = (await client.get("/api/me/dashboard", headers=ALICE_HEADERS)).json()
    unread = [c for c in dashboard["unread"] if c["channel_id"] == channel_id]
    assert unread and unread[0]["new_posts"] >= 1


@pytest.mark.asyncio
async def test_numeric_string_channel_id_is_coerced(client: AsyncClient):
    """Pydantic's lax int coercion is the codebase-wide idiom (every other id
    field behaves this way); pin it deliberately: "1" targets channel 1 and is
    echoed back as the persisted int, never silently dropped."""
    _, channel_id = await _alice_group_channel(client)

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "String variant",
            "body_markdown": "channel_id sent as a numeric string.",
            "channel_id": str(channel_id),
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["channel_id"] == channel_id


@pytest.mark.asyncio
async def test_reply_inherits_parent_channel(client: AsyncClient):
    """A reply via parent_post_id with channel_id omitted inherits the
    parent's channel — the #168 inheritance gap made these orphans."""
    _, channel_id = await _alice_group_channel(client)

    root = (
        await client.post(
            "/api/posts",
            json={
                "subject": "Root",
                "body_markdown": "Root body in a channel.",
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
    ).json()["id"]

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Reply",
            "body_markdown": "Reply body, channel inherited from parent.",
            "parent_post_id": root,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    reply_id = resp.json()["id"]
    assert resp.json()["channel_id"] == channel_id

    async with TestSession() as session:
        persisted = (
            await session.execute(select(Post.channel_id).where(Post.id == reply_id))
        ).scalar_one()
    assert persisted == channel_id

    listing = await client.get(f"/api/channels/{channel_id}/messages", headers=ALICE_HEADERS)
    assert reply_id in [p["id"] for p in listing.json()]


@pytest.mark.asyncio
async def test_reply_channel_id_must_match_parent(client: AsyncClient):
    """An explicit reply channel that differs from the parent's would split a
    thread across channels — reject with 400, mirroring the same-channel rule
    on POST /api/channels/{id}/messages."""
    _, channel_a = await _alice_group_channel(client)
    channel_b = await _second_channel(client)

    root = (
        await client.post(
            "/api/posts",
            json={
                "subject": "Root",
                "body_markdown": "Root body in channel A.",
                "channel_id": channel_a,
            },
            headers=ALICE_HEADERS,
        )
    ).json()["id"]

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Cross-thread reply",
            "body_markdown": "Explicitly targeting a different channel.",
            "parent_post_id": root,
            "channel_id": channel_b,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reply_to_unchanneled_parent_requires_channel_id(client: AsyncClient):
    """Legacy NULL-channel parents (pre-#168 rows) have nothing to inherit;
    the reply must then carry an explicit channel_id."""
    async with TestSession() as session:
        legacy = Post(
            author="alice@herd.ai",
            subject="Legacy orphan parent",
            tldr="t",
            body_markdown="b",
            body_html="<p>b</p>",
            token_cost=1,
            channel_id=None,
        )
        session.add(legacy)
        await session.commit()
        parent_id = legacy.id

    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Reply without channel",
            "body_markdown": "Nothing to inherit here.",
            "parent_post_id": parent_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 400

    _, channel_id = await _alice_group_channel(client)
    resp = await client.post(
        "/api/posts",
        json={
            "subject": "Reply with explicit channel",
            "body_markdown": "Explicit channel on a legacy orphan parent.",
            "parent_post_id": parent_id,
            "channel_id": channel_id,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["channel_id"] == channel_id


@pytest.mark.asyncio
async def test_creations_leave_zero_orphan_rows(client: AsyncClient):
    """The post-51 invariant: after any mix of creates, no non-deleted row
    carries a NULL channel_id."""
    _, channel_id = await _alice_group_channel(client)

    root = (
        await client.post(
            "/api/posts",
            json={
                "subject": "Root",
                "body_markdown": "Standalone with explicit channel.",
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
    ).json()["id"]

    await client.post(
        "/api/posts",
        json={
            "subject": "Reply one",
            "body_markdown": "Inherits the root's channel.",
            "parent_post_id": root,
        },
        headers=ALICE_HEADERS,
    )

    async with TestSession() as session:
        orphans = (
            await session.execute(
                select(Post.id).where(
                    Post.channel_id.is_(None),
                    Post.status.notin_(("archived", "deleted")),
                )
            )
        ).all()
    assert orphans == []


@pytest.mark.asyncio
async def test_channel_id_visible_across_read_surfaces(client: AsyncClient):
    """Detail GET and the thread view must echo channel_id — the omission is
    what made Cheng Yi's client render a persisted channel as null."""
    _, channel_id = await _alice_group_channel(client)

    post_id = (
        await client.post(
            "/api/posts",
            json={
                "subject": "Surface consistency",
                "body_markdown": "Detail and thread views must agree.",
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
    ).json()["id"]

    detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
    assert detail["channel_id"] == channel_id

    thread = (await client.get(f"/api/posts/{post_id}/thread", headers=ALICE_HEADERS)).json()
    assert thread["post"]["channel_id"] == channel_id
