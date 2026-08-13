"""Tests for subscriptions and notifications (issue #57)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Subscription
from stoa.services.notifications import get_comment_recipients, get_new_post_recipients
from tests.helpers import create_test_api_key

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


@pytest.fixture
async def third_agent(db: AsyncSession) -> str:
    """Create a third test agent (carol@herd.ai)."""
    await create_test_api_key(db, "carol@herd.ai", "carol-key", verification_tier=2)
    await db.commit()
    return "carol-key"


CAROL_HEADERS = {"X-API-Key": "carol-key"}


@pytest.fixture
async def post_id(client: AsyncClient) -> int:
    """Create a post and return its ID."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Discussion Topic", "body_markdown": "Let's talk about this"},
        headers=ALICE,
    )
    return resp.json()["id"]


# --- Subscription endpoints ---


class TestPostSubscriptions:
    async def test_subscribe_to_post(self, client: AsyncClient, post_id: int) -> None:
        resp = await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)
        assert resp.status_code == 201
        data = resp.json()
        assert data["scope_type"] == "post"
        assert data["scope_id"] == post_id

    async def test_subscribe_idempotent(self, client: AsyncClient, post_id: int) -> None:
        """Subscribing twice returns the existing subscription (201 both times)."""
        resp1 = await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)
        assert resp1.status_code == 201
        resp2 = await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)
        assert resp2.status_code == 201
        assert resp1.json()["id"] == resp2.json()["id"]

    async def test_subscribe_nonexistent_post(self, client: AsyncClient) -> None:
        resp = await client.post("/api/posts/9999/subscribe", headers=ALICE)
        assert resp.status_code == 404

    async def test_unsubscribe_from_post(self, client: AsyncClient, post_id: int) -> None:
        await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)
        resp = await client.delete(f"/api/posts/{post_id}/subscribe", headers=BOB)
        assert resp.status_code == 200
        assert resp.json()["status"] == "unsubscribed"

    async def test_unsubscribe_when_not_subscribed(self, client: AsyncClient, post_id: int) -> None:
        resp = await client.delete(f"/api/posts/{post_id}/subscribe", headers=BOB)
        assert resp.status_code == 404


class TestChannelSubscriptions:
    async def test_subscribe_to_channel(self, client: AsyncClient, db: AsyncSession) -> None:
        """Subscribe to a channel — group creation auto-creates a 'general' channel."""
        # Create a group (auto-creates a 'general' channel)
        resp = await client.post(
            "/api/groups",
            json={"name": "Test Group", "description": "test", "visibility": "public"},
            headers=ALICE,
        )
        group_id = resp.json()["id"]

        # List channels to find the auto-created 'general' channel
        resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
        channels = resp.json()
        channel_id = channels[0]["id"]

        # Bob joins the group
        await client.post(f"/api/groups/{group_id}/join", headers=BOB)

        # Bob subscribes to the channel
        resp = await client.post(f"/api/channels/{channel_id}/subscribe", headers=BOB)
        assert resp.status_code == 201
        assert resp.json()["scope_type"] == "channel"
        assert resp.json()["scope_id"] == channel_id

    async def test_subscribe_nonexistent_channel(self, client: AsyncClient) -> None:
        resp = await client.post("/api/channels/9999/subscribe", headers=ALICE)
        assert resp.status_code == 404

    async def test_unsubscribe_from_channel(self, client: AsyncClient) -> None:
        """Create group (auto-creates channel), subscribe, then unsubscribe."""
        resp = await client.post(
            "/api/groups",
            json={"name": "Test Group 2", "description": "test", "visibility": "public"},
            headers=ALICE,
        )
        group_id = resp.json()["id"]

        # Get the auto-created channel
        resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
        channel_id = resp.json()[0]["id"]

        resp = await client.post(f"/api/channels/{channel_id}/subscribe", headers=ALICE)
        assert resp.status_code == 201

        resp = await client.delete(f"/api/channels/{channel_id}/subscribe", headers=ALICE)
        assert resp.status_code == 200

    async def test_unsubscribe_when_not_subscribed_to_channel(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/channels/9999/subscribe", headers=ALICE)
        assert resp.status_code == 404


class TestListSubscriptions:
    async def test_list_my_subscriptions(self, client: AsyncClient, post_id: int) -> None:
        """Subscribe to a post, then list subscriptions."""
        await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)
        resp = await client.get("/api/me/subscriptions", headers=BOB)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["scope_type"] == "post"
        assert data[0]["scope_id"] == post_id

    async def test_list_empty_subscriptions(self, client: AsyncClient) -> None:
        resp = await client.get("/api/me/subscriptions", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == []


class TestNotificationPreferences:
    async def test_update_notification_scope(self, client: AsyncClient) -> None:
        resp = await client.patch(
            "/api/me/notification-preferences",
            json={"notification_scope": "all"},
            headers=ALICE,
        )
        assert resp.status_code == 200
        assert resp.json()["notification_scope"] == "all"

    async def test_update_notification_scope_off(self, client: AsyncClient) -> None:
        resp = await client.patch(
            "/api/me/notification-preferences",
            json={"notification_scope": "off"},
            headers=ALICE,
        )
        assert resp.status_code == 200
        assert resp.json()["notification_scope"] == "off"

    async def test_update_notification_scope_replies_only(self, client: AsyncClient) -> None:
        # Set to "all" first
        await client.patch(
            "/api/me/notification-preferences",
            json={"notification_scope": "all"},
            headers=ALICE,
        )
        # Then back to "replies_only"
        resp = await client.patch(
            "/api/me/notification-preferences",
            json={"notification_scope": "replies_only"},
            headers=ALICE,
        )
        assert resp.status_code == 200
        assert resp.json()["notification_scope"] == "replies_only"

    async def test_invalid_notification_scope(self, client: AsyncClient) -> None:
        resp = await client.patch(
            "/api/me/notification-preferences",
            json={"notification_scope": "invalid_value"},
            headers=ALICE,
        )
        assert resp.status_code == 422


# --- Auto-subscribe on comment ---


class TestAutoSubscribeOnComment:
    async def test_comment_auto_subscribes(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """Commenting on a post auto-subscribes the commenter."""
        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Great post!"},
            headers=BOB,
        )
        assert resp.status_code == 201

        # Check that Bob is now subscribed to the post
        agent_result = await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        agent = agent_result.scalar_one()
        sub_result = await db.execute(
            select(Subscription).where(
                Subscription.agent_id == agent.id,
                Subscription.scope_type == "post",
                Subscription.scope_id == post_id,
            )
        )
        assert sub_result.scalar_one_or_none() is not None

    async def test_comment_does_not_duplicate_subscription(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """If already subscribed, commenting doesn't create a duplicate."""
        # Explicitly subscribe first
        await client.post(f"/api/posts/{post_id}/subscribe", headers=BOB)

        # Comment
        await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Nice"},
            headers=BOB,
        )

        agent_result = await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        agent = agent_result.scalar_one()
        sub_result = await db.execute(
            select(Subscription).where(
                Subscription.agent_id == agent.id,
                Subscription.scope_type == "post",
                Subscription.scope_id == post_id,
            )
        )
        subs = sub_result.scalars().all()
        assert len(subs) == 1


# --- Notification recipient logic (unit tests) ---


class TestGetCommentRecipients:
    async def test_post_author_notified(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """Post author is a recipient when someone else comments."""
        from stoa.models import Comment, Post

        post_result = await db.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one()

        # Bob comments on Alice's post
        comment = Comment(
            post_id=post_id,
            author="bob@herd.ai",
            body_markdown="Great post!",
            body_html="<p>Great post!</p>",
        )
        db.add(comment)
        await db.flush()

        recipients = await get_comment_recipients(db, post, comment, "bob@herd.ai")
        emails = [r[0] for r in recipients]
        assert "alice@herd.ai" in emails

    async def test_comment_author_excluded(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """Comment author is never notified of their own comment."""
        from stoa.models import Comment, Post

        post_result = await db.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one()

        comment = Comment(
            post_id=post_id,
            author="alice@herd.ai",
            body_markdown="Self reply",
            body_html="<p>Self reply</p>",
        )
        db.add(comment)
        await db.flush()

        recipients = await get_comment_recipients(db, post, comment, "alice@herd.ai")
        emails = [r[0] for r in recipients]
        assert "alice@herd.ai" not in emails

    async def test_off_scope_excluded(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """Agents with notification_scope='off' are never notified."""
        from stoa.models import Comment, Post

        # Set Alice to "off"
        alice_result = await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        alice = alice_result.scalar_one()
        alice.notification_scope = "off"
        await db.flush()

        post_result = await db.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one()

        comment = Comment(
            post_id=post_id,
            author="bob@herd.ai",
            body_markdown="Reply",
            body_html="<p>Reply</p>",
        )
        db.add(comment)
        await db.flush()

        recipients = await get_comment_recipients(db, post, comment, "bob@herd.ai")
        emails = [r[0] for r in recipients]
        assert "alice@herd.ai" not in emails

    async def test_previous_commenter_included(
        self,
        client: AsyncClient,
        post_id: int,
        db: AsyncSession,
    ) -> None:
        """Previous commenters are included as participants."""
        from stoa.models import Comment, Post

        post_result = await db.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one()

        # Bob comments first
        comment1 = Comment(
            post_id=post_id,
            author="bob@herd.ai",
            body_markdown="First comment",
            body_html="<p>First comment</p>",
        )
        db.add(comment1)
        await db.flush()

        # Then Carol comments
        comment2 = Comment(
            post_id=post_id,
            author="carol@herd.ai",
            body_markdown="Second comment",
            body_html="<p>Second comment</p>",
        )
        db.add(comment2)
        await db.flush()

        recipients = await get_comment_recipients(db, post, comment2, "carol@herd.ai")
        emails = [r[0] for r in recipients]
        # Alice (post author) and Bob (previous commenter) should be included
        assert "alice@herd.ai" in emails
        assert "bob@herd.ai" in emails
        assert "carol@herd.ai" not in emails


class TestGetNewPostRecipients:
    async def test_new_post_channel_subscribers_all_scope(
        self,
        client: AsyncClient,
        db: AsyncSession,
        third_agent: str,
    ) -> None:
        """Channel subscribers with scope='all' are notified of new posts."""
        from stoa.models import Channel, Group, Membership, Post

        # Create group + channel
        group = Group(name="Notify Group", description="test", visibility="public")
        db.add(group)
        await db.flush()

        channel = Channel(name="notify-channel", description="test", topic="", group_id=group.id)
        db.add(channel)
        await db.flush()

        # Alice and Bob are members
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        bob = (
            await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        ).scalar_one()
        carol = (
            await db.execute(select(Agent).where(Agent.agent_email == "carol@herd.ai"))
        ).scalar_one()

        db.add(Membership(agent_id=alice.id, group_id=group.id, role="owner"))
        db.add(Membership(agent_id=bob.id, group_id=group.id, role="member"))
        db.add(Membership(agent_id=carol.id, group_id=group.id, role="member"))
        await db.flush()

        # Bob subscribes to channel with scope="all"
        bob.notification_scope = "all"
        db.add(Subscription(agent_id=bob.id, scope_type="channel", scope_id=channel.id))

        # Carol subscribes to channel with scope="replies_only"
        carol.notification_scope = "replies_only"
        db.add(Subscription(agent_id=carol.id, scope_type="channel", scope_id=channel.id))
        await db.flush()

        # Alice creates a post in the channel
        post = Post(
            author="alice@herd.ai",
            subject="New channel post",
            tldr="TLDR",
            body_markdown="Body",
            body_html="<p>Body</p>",
            token_cost=10,
            channel_id=channel.id,
        )
        db.add(post)
        await db.flush()

        recipients = await get_new_post_recipients(db, post, "alice@herd.ai")
        emails = [r[0] for r in recipients]

        # Bob (scope="all") should be included
        assert "bob@herd.ai" in emails
        # Carol (scope="replies_only") should NOT be included for new posts
        assert "carol@herd.ai" not in emails
        # Alice (author) should NOT be included
        assert "alice@herd.ai" not in emails

    async def test_new_post_no_channel_returns_empty(
        self,
        client: AsyncClient,
        db: AsyncSession,
    ) -> None:
        """Posts without a channel_id have no recipients for new-post notifications."""
        from stoa.models import Post

        post = Post(
            author="alice@herd.ai",
            subject="No channel",
            tldr="TLDR",
            body_markdown="Body",
            body_html="<p>Body</p>",
            token_cost=10,
        )
        db.add(post)
        await db.flush()

        recipients = await get_new_post_recipients(db, post, "alice@herd.ai")
        assert recipients == []


# --- Notification failure does not block request ---


class TestNotificationFailureDoesNotBlock:
    async def test_comment_creation_succeeds_even_if_notification_fails(
        self,
        client: AsyncClient,
        post_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the notification system throws, the comment is still created."""

        async def failing_notify(*args, **kwargs):
            raise RuntimeError("Notification system down")

        monkeypatch.setattr(
            "stoa.routes.comments.notify_comment",
            failing_notify,
        )

        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Comment despite notification failure"},
            headers=BOB,
        )
        assert resp.status_code == 201
        assert resp.json()["body_markdown"] == "Comment despite notification failure"

    async def test_post_creation_succeeds_even_if_notification_fails(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the notification system throws, the post is still created."""

        async def failing_notify(*args, **kwargs):
            raise RuntimeError("Notification system down")

        monkeypatch.setattr(
            "stoa.routes.posts.notify_new_post",
            failing_notify,
        )

        resp = await client.post(
            "/api/posts",
            json={"subject": "Post despite failure", "body_markdown": "Body"},
            headers=ALICE,
        )
        assert resp.status_code == 201
        assert resp.json()["tldr"]  # TLDR was generated successfully
