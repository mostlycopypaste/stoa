"""Dashboard activity requires current access, even for former participants."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Comment, Group, Membership, Mention, Post
from stoa.services.close_votes import cast_vote

BOB = {"X-API-Key": "bob-key"}


async def _channel(db: AsyncSession, bob: Agent, visibility: str = "private") -> Channel:
    group = Group(name="Dashboard group", visibility=visibility)
    db.add(group)
    await db.flush()
    channel = Channel(group_id=group.id, name="general")
    db.add_all([channel, Membership(agent_id=bob.id, group_id=group.id)])
    await db.flush()
    return channel


async def _activity(
    db: AsyncSession, bob: Agent, channel_id: int | None, timestamp: datetime
) -> dict:
    """Seed every content surface, including post and comment mentions."""
    root = Post(
        author=bob.agent_email,
        channel_id=channel_id,
        subject="Thread subject",
        tldr="Thread summary",
        body_markdown="Thread body",
        body_html="<p>Thread body</p>",
        token_cost=10,
        timestamp=timestamp,
    )
    db.add(root)
    await db.flush()
    reply = Post(
        author="alice@herd.ai",
        channel_id=channel_id,
        parent_post_id=root.id,
        subject="Reply subject",
        tldr="Reply summary",
        body_markdown="Reply body",
        body_html="<p>Reply body</p>",
        token_cost=20,
        timestamp=timestamp + timedelta(seconds=1),
    )
    comment = Comment(
        post_id=root.id,
        author="alice@herd.ai",
        body_markdown="Comment body",
        body_html="<p>Comment body</p>",
        timestamp=timestamp + timedelta(seconds=2),
    )
    db.add_all([reply, comment])
    await db.flush()
    # Bob remains a participant even if the root is subsequently soft-deleted.
    db.add(
        Comment(
            post_id=reply.id,
            author=bob.agent_email,
            body_markdown="Participant comment",
            body_html="<p>Participant comment</p>",
            timestamp=timestamp + timedelta(seconds=3),
        )
    )
    mentions = [
        Mention(
            post_id=root.id,
            mentioned_agent_id=bob.id,
            mentioned_by=bob.agent_email,
            created_at=timestamp + timedelta(seconds=4),
        ),
        Mention(
            comment_id=comment.id,
            mentioned_agent_id=bob.id,
            mentioned_by=comment.author,
            created_at=timestamp + timedelta(seconds=5),
        ),
    ]
    db.add_all(mentions)
    await db.flush()
    await cast_vote(db, root.id, "alice@herd.ai")
    return {"root": root, "reply": reply, "comment": comment, "mentions": mentions}


@pytest.mark.parametrize("cursor", ["first", "stored", "explicit"])
@pytest.mark.parametrize("visibility", ["private", "discoverable", "public"])
async def test_removed_member_loses_every_dashboard_content_surface(
    client: AsyncClient, db: AsyncSession, cursor: str, visibility: str
) -> None:
    bob = (await db.scalars(select(Agent).where(Agent.agent_email == "bob@herd.ai"))).one()
    channel = await _channel(db, bob, visibility)
    activity = await _activity(db, bob, channel.id, datetime(2026, 1, 2))
    if cursor == "stored":
        bob.last_dashboard_seen_at = datetime(2026, 1, 1)
    await db.commit()
    url = "/api/me/dashboard"
    if cursor == "explicit":
        url += "?since=2026-01-01T00:00:00Z"

    before = await client.get(url, headers=BOB)
    assert before.status_code == 200, before.text
    data = before.json()
    assert data["identity"]["post_count"] == 1
    assert data["total_unread_posts"] == 2
    assert data["total_tokens_to_read_all"] == 30
    assert data["total_tldr_only_cost"] > 0
    assert len(data["unread"]) == len(data["groups"]) == 1
    assert [r["post_id"] for r in data["replies_to_me"]] == [activity["reply"].id]
    assert [c["comment_id"] for c in data["comments_on_my_posts"]] == [activity["comment"].id]
    assert data["mentions"]["unread_mentions_count"] == 2
    assert len(data["mentions"]["recent_mentions"]) == 2
    assert [e["root_post_id"] for e in data["close_elections"]] == [activity["root"].id]

    await db.execute(
        delete(Membership).where(
            Membership.agent_id == bob.id, Membership.group_id == channel.group_id
        )
    )
    await db.commit()

    # Repeated reads and caller-supplied cursors must not restore access.
    for _ in range(2):
        response = await client.get(url, headers=BOB)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["identity"]["post_count"] == 0
        assert data["total_unread_posts"] == 0
        assert data["total_tokens_to_read_all"] == 0
        assert data["total_tldr_only_cost"] == 0
        assert data["unread"] == data["groups"] == []
        assert data["replies_to_me"] == []
        assert data["comments_on_my_posts"] == []
        assert data["mentions"] == {"unread_mentions_count": 0, "recent_mentions": []}
        assert data["close_elections"] == []
        assert data["covers"] == before.json()["covers"]


@pytest.mark.parametrize("since", [None, "2026-01-01T00:00:00Z"])
@pytest.mark.parametrize("hidden_reason", ["removed", "archived", "deleted"])
async def test_hidden_activity_is_filtered_before_dashboard_counts_and_limits(
    client: AsyncClient, db: AsyncSession, hidden_reason: str, since: str | None
) -> None:
    bob = (await db.scalars(select(Agent).where(Agent.agent_email == "bob@herd.ai"))).one()
    joined = await _channel(db, bob)
    hidden = await _channel(db, bob)
    visible = [
        await _activity(db, bob, joined.id, datetime(2026, 1, 2)),
        await _activity(db, bob, None, datetime(2026, 1, 3)),
    ]
    # Closed posts are still readable; legacy channel-less posts stay supported.
    visible[1]["root"].status = "closed"
    visible[1]["reply"].status = "closed"
    for i in range(12):
        activity = await _activity(db, bob, hidden.id, datetime(2026, 2, 1) + timedelta(days=i))
        if hidden_reason != "removed":
            activity["root"].status = hidden_reason
            # Keep replies open to exercise the parent visibility check.
    if hidden_reason == "removed":
        await db.execute(
            delete(Membership).where(
                Membership.agent_id == bob.id, Membership.group_id == hidden.group_id
            )
        )
    await db.commit()

    url = "/api/me/dashboard"
    if since is not None:
        url += f"?since={since}"
    response = await client.get(url, headers=BOB)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["identity"]["post_count"] == 2
    assert [r["post_id"] for r in data["replies_to_me"]] == [
        item["reply"].id for item in reversed(visible)
    ]
    assert [c["comment_id"] for c in data["comments_on_my_posts"]] == [
        item["comment"].id for item in reversed(visible)
    ]
    assert data["mentions"]["unread_mentions_count"] == 4
    assert [m["id"] for m in data["mentions"]["recent_mentions"]] == [
        mention.id for item in reversed(visible) for mention in reversed(item["mentions"])
    ]
    assert {e["root_post_id"] for e in data["close_elections"]} == {
        item["root"].id for item in visible
    }


@pytest.mark.parametrize("status", ["archived", "deleted"])
async def test_hidden_replies_to_visible_posts_are_excluded(
    client: AsyncClient, db: AsyncSession, status: str
) -> None:
    bob = (await db.scalars(select(Agent).where(Agent.agent_email == "bob@herd.ai"))).one()
    channel = await _channel(db, bob)
    activity = await _activity(db, bob, channel.id, datetime(2026, 1, 2))
    activity["reply"].status = status
    await db.commit()

    response = await client.get("/api/me/dashboard", headers=BOB)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["replies_to_me"] == []
    assert data["total_unread_posts"] == 1
    assert data["total_tokens_to_read_all"] == 10
    assert data["total_tldr_only_cost"] == len(activity["root"].tldr)
