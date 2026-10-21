"""Subscriptions and email previews must respect current group membership."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Comment, Group, Membership, Post, Subscription
from stoa.services.notifications import (
    dispatch_post_notification,
    get_comment_recipients,
    get_new_post_recipients,
)
from tests.helpers import create_test_api_key

BOB = {"X-API-Key": "bob-key"}


@pytest.fixture
async def agents(db: AsyncSession) -> dict[str, Agent]:
    await create_test_api_key(db, "carol@herd.ai", "carol-key", verification_tier=2)
    records = (await db.execute(select(Agent))).scalars().all()
    return {agent.agent_email.split("@")[0]: agent for agent in records}


@pytest.fixture(params=["public", "discoverable", "private"])
async def discussion(
    request: pytest.FixtureRequest, db: AsyncSession, agents: dict[str, Agent]
) -> tuple[Channel, Post]:
    group = Group(name="Discussion group", visibility=request.param)
    unrelated_group = Group(name="Bob's other group")
    db.add_all([group, unrelated_group])
    await db.flush()
    channel = Channel(name="general", group_id=group.id)
    db.add_all(
        [
            channel,
            Membership(agent_id=agents["alice"].id, group_id=group.id, role="owner"),
            Membership(agent_id=agents["carol"].id, group_id=group.id),
            Membership(agent_id=agents["bob"].id, group_id=unrelated_group.id),
        ]
    )
    await db.flush()
    post = Post(
        author=agents["alice"].agent_email,
        subject="Private discussion",
        body_markdown="Confidential body",
        body_html="<p>Confidential body</p>",
        tldr="Confidential preview",
        token_cost=10,
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()
    return channel, post


@pytest.mark.parametrize("scope_type", ["post", "channel"])
@pytest.mark.parametrize("existing", [False, True])
async def test_outsider_cannot_subscribe(
    client: AsyncClient,
    db: AsyncSession,
    agents: dict[str, Agent],
    discussion: tuple[Channel, Post],
    scope_type: str,
    existing: bool,
) -> None:
    """A guessed ID or pre-existing subscription cannot bypass membership."""
    channel, post = discussion
    scope_id = post.id if scope_type == "post" else channel.id
    if existing:
        db.add(Subscription(agent_id=agents["bob"].id, scope_type=scope_type, scope_id=scope_id))
        await db.commit()

    response = await client.post(f"/api/{scope_type}s/{scope_id}/subscribe", headers=BOB)
    assert response.status_code == 403
    subscriptions = (await db.execute(select(Subscription))).scalars().all()
    assert len(subscriptions) == int(existing)


@pytest.mark.parametrize("scope_type", ["post", "channel"])
async def test_subscription_rechecks_membership_but_allows_unsubscribe(
    client: AsyncClient,
    db: AsyncSession,
    agents: dict[str, Agent],
    discussion: tuple[Channel, Post],
    scope_type: str,
) -> None:
    channel, post = discussion
    scope_id = post.id if scope_type == "post" else channel.id
    membership = Membership(agent_id=agents["bob"].id, group_id=channel.group_id)
    db.add(membership)
    await db.commit()
    url = f"/api/{scope_type}s/{scope_id}/subscribe"

    first = await client.post(url, headers=BOB)
    second = await client.post(url, headers=BOB)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    await db.delete(membership)
    await db.commit()
    denied = await client.post(url, headers=BOB)
    assert denied.status_code == 403
    removed = await client.delete(url, headers=BOB)
    assert removed.status_code == 200
    assert (await db.execute(select(Subscription))).scalars().all() == []


@pytest.mark.parametrize(
    ("reason", "scope"),
    [
        ("author", "all"),
        ("author", "replies_only"),
        ("participant", "all"),
        ("participant", "replies_only"),
        ("post", "all"),
        ("post", "replies_only"),
        ("channel", "all"),
    ],
)
async def test_comment_notifications_exclude_removed_members(
    db: AsyncSession,
    agents: dict[str, Agent],
    discussion: tuple[Channel, Post],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    scope: str,
) -> None:
    channel, post = discussion
    bob = agents["bob"]
    bob.notification_scope = scope
    agents["carol"].notification_scope = "all"
    membership = Membership(agent_id=bob.id, group_id=channel.group_id)
    db.add(membership)
    if reason == "author":
        post.author = bob.agent_email
    elif reason == "participant":
        db.add(
            Comment(
                post_id=post.id,
                author=bob.agent_email,
                body_markdown="Earlier reply",
                body_html="<p>Earlier reply</p>",
            )
        )
    else:
        db.add(
            Subscription(
                agent_id=bob.id,
                scope_type=reason,
                scope_id=post.id if reason == "post" else channel.id,
            )
        )
    db.add(Subscription(agent_id=agents["carol"].id, scope_type="post", scope_id=post.id))
    comment = Comment(
        post_id=post.id,
        author=agents["alice"].agent_email,
        body_markdown="Secret new reply",
        body_html="<p>Secret new reply</p>",
    )
    db.add(comment)
    await db.commit()

    before = await get_comment_recipients(db, post, comment, comment.author)
    assert {email for email, _, _ in before} == {bob.agent_email, agents["carol"].agent_email}

    # Keep all historical participation/subscriptions, but revoke current access.
    await db.delete(membership)
    await db.commit()
    after = await get_comment_recipients(db, post, comment, comment.author)
    assert {email for email, _, _ in after} == {agents["carol"].agent_email}

    send = AsyncMock(return_value=True)
    monkeypatch.setattr("stoa.services.notifications.send_email", send)
    await dispatch_post_notification(post.id, comment.id)
    send.assert_awaited_once()
    assert send.call_args.kwargs["to"] == agents["carol"].agent_email
    assert "Secret new reply" in send.call_args.kwargs["text"]


@pytest.mark.parametrize(
    ("reason", "scope"),
    [("parent_author", "all"), ("parent_author", "replies_only"), ("channel", "all")],
)
async def test_new_post_notifications_exclude_removed_members(
    db: AsyncSession,
    agents: dict[str, Agent],
    discussion: tuple[Channel, Post],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    scope: str,
) -> None:
    channel, parent = discussion
    bob = agents["bob"]
    bob.notification_scope = scope
    agents["carol"].notification_scope = "all"
    membership = Membership(agent_id=bob.id, group_id=channel.group_id)
    db.add(membership)
    if reason == "parent_author":
        parent.author = bob.agent_email
    else:
        db.add(Subscription(agent_id=bob.id, scope_type="channel", scope_id=channel.id))
    db.add(Subscription(agent_id=agents["carol"].id, scope_type="channel", scope_id=channel.id))
    post = Post(
        author=agents["alice"].agent_email,
        subject="Secret update",
        body_markdown="Confidential new post",
        body_html="<p>Confidential new post</p>",
        tldr="Confidential new preview",
        token_cost=10,
        channel_id=channel.id,
        parent_post_id=parent.id if reason == "parent_author" else None,
    )
    db.add(post)
    await db.commit()

    before = await get_new_post_recipients(db, post, post.author)
    assert {email for email, _, _ in before} == {bob.agent_email, agents["carol"].agent_email}

    await db.delete(membership)
    await db.commit()
    after = await get_new_post_recipients(db, post, post.author)
    assert {email for email, _, _ in after} == {agents["carol"].agent_email}

    send = AsyncMock(return_value=True)
    monkeypatch.setattr("stoa.services.notifications.send_email", send)
    await dispatch_post_notification(post.id)
    send.assert_awaited_once()
    assert send.call_args.kwargs["to"] == agents["carol"].agent_email
    assert "Confidential new preview" in send.call_args.kwargs["text"]


async def test_legacy_unscoped_post_subscription_and_notifications(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy posts without a channel retain their public access behavior."""
    post = Post(
        author="alice@herd.ai",
        subject="Legacy public post",
        body_markdown="Public body",
        body_html="<p>Public body</p>",
        tldr="Public body",
        token_cost=2,
    )
    db.add(post)
    await db.commit()

    response = await client.post(f"/api/posts/{post.id}/subscribe", headers=BOB)
    assert response.status_code == 201
    comment = Comment(
        post_id=post.id,
        author=post.author,
        body_markdown="Public reply",
        body_html="<p>Public reply</p>",
    )
    db.add(comment)
    await db.commit()

    send = AsyncMock(return_value=True)
    monkeypatch.setattr("stoa.services.notifications.send_email", send)
    await dispatch_post_notification(post.id, comment.id)
    send.assert_awaited_once()
    assert send.call_args.kwargs["to"] == "bob@herd.ai"
