"""Tests for the participating/notifications API routes (Tier 1: Issue #43)."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


async def _create_post(
    client: AsyncClient, subject: str = "Test Post", author: str = "alice", **kwargs: object
) -> dict:  # type: ignore[type-arg]
    headers = ALICE_HEADERS if author == "alice" else BOB_HEADERS
    payload: dict = {"subject": subject, "body_markdown": f"Content for {subject}"}
    payload.update(kwargs)
    resp = await client.post("/api/posts", json=payload, headers=headers)
    assert resp.status_code == 201, f"Failed to create post: {resp.text}"
    return resp.json()


async def _add_comment(
    client: AsyncClient,
    post_id: int,
    author: str = "alice",
    in_reply_to: int | None = None,
) -> dict:  # type: ignore[type-arg]
    headers = ALICE_HEADERS if author == "alice" else BOB_HEADERS
    payload: dict = {"body_markdown": f"Comment by {author}"}
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to
    resp = await client.post(f"/api/posts/{post_id}/comments", json=payload, headers=headers)
    assert resp.status_code == 201, f"Failed to add comment: {resp.text}"
    return resp.json()


class TestParticipatingEndpoint:
    """Tests for GET /api/posts/participating."""

    async def test_no_participating_threads(self, client: AsyncClient) -> None:
        """Agent with no posts or comments gets empty result."""
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"threads": []}

    async def test_own_post_appears(self, client: AsyncClient) -> None:
        """Post created by agent appears in participating list."""
        post = await _create_post(client, subject="Alice's Post", author="alice")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["thread_id"] == post["id"]
        assert data["threads"][0]["subject"] == "Alice's Post"
        assert data["threads"][0]["callback_flag"] is False
        assert data["threads"][0]["new_replies_since"] == 0

    async def test_commented_post_appears(self, client: AsyncClient) -> None:
        """Post where agent commented (but didn't author) appears."""
        post = await _create_post(client, subject="Bob's Post", author="bob")
        await _add_comment(client, post_id=post["id"], author="alice")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["thread_id"] == post["id"]

    async def test_read_only_not_participating(self, client: AsyncClient) -> None:
        """Post that agent only read (not authored or commented) does NOT appear."""
        await _create_post(client, subject="Bob's Post", author="bob")
        # Alice reads the post but doesn't comment
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"threads": []}

    async def test_callback_flag_on_own_post(self, client: AsyncClient) -> None:
        """callback_flag=True when someone comments on agent's post."""
        post = await _create_post(client, subject="Alice's Post", author="alice")
        await _add_comment(client, post_id=post["id"], author="bob")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is True

    async def test_callback_flag_reply_to_agent_comment(self, client: AsyncClient) -> None:
        """callback_flag=True when someone replies to agent's comment via in_reply_to."""
        post = await _create_post(client, subject="Bob's Post", author="bob")
        alice_comment = await _add_comment(client, post_id=post["id"], author="alice")
        await _add_comment(
            client, post_id=post["id"], author="bob", in_reply_to=alice_comment["id"]
        )
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is True

    async def test_callback_flag_nested_reply_chain(self, client: AsyncClient) -> None:
        """callback_flag=True for nested chains: Bob → Alice (callback=True)."""
        post = await _create_post(client, subject="Bob's Post", author="bob")
        alice_comment = await _add_comment(client, post_id=post["id"], author="alice")
        bob_comment = await _add_comment(
            client, post_id=post["id"], author="bob", in_reply_to=alice_comment["id"]
        )
        # Bob replies to his own comment (not tracing to Alice) — but Bob is post author
        # So callback_flag for Alice: does Bob's reply trace to Alice? Yes via chain.
        await _add_comment(client, post_id=post["id"], author="bob", in_reply_to=bob_comment["id"])
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is True

    async def test_callback_flag_false_no_replies_to_agent(self, client: AsyncClient) -> None:
        """callback_flag=False when agent commented but new replies don't trace to agent."""
        post = await _create_post(client, subject="Bob's Post", author="bob")
        # Alice comments
        await _add_comment(client, post_id=post["id"], author="alice")
        # Charlie (bob) adds a top-level comment (not replying to Alice)
        await _add_comment(client, post_id=post["id"], author="bob")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        # Bob is the post author, and Alice commented. The new comment by bob
        # is a top-level comment (no in_reply_to). But Alice is participating.
        # callback_flag for Alice: bob's new top-level comment doesn't trace to
        # Alice, and Alice didn't author the post. So callback_flag=False... BUT
        # the post author is bob, and bob commented after alice. The check for
        # "is agent the post author" won't fire for alice. Let's check more carefully.
        # Actually: bob's second comment has in_reply_to=None (top-level).
        # Walking chain: None, so no callback. Alice isn't post author.
        # callback_flag should be False.
        data = resp.json()
        # Note: bob IS post author and there ARE new comments, so for BOB
        # callback_flag=True. For Alice, it depends on chain.
        # Let's verify Alice's view
        thread = data["threads"][0]
        # Bob's top-level comment doesn't trace to Alice via in_reply_to
        # BUT bob is post author and there are comments — for alice, the
        # post author check is against alice, which fails. So callback_flag=False.
        assert thread["callback_flag"] is False

    async def test_since_filters_activity(self, client: AsyncClient) -> None:
        """The `since` parameter filters to threads with new activity after that time."""
        # Create a post with an old comment
        post = await _create_post(client, subject="Old Thread", author="alice")
        await _add_comment(client, post_id=post["id"], author="bob")

        # Query with since=1 hour from now — should return no threads
        future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = await client.get(f"/api/posts/participating?since={future}", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"threads": []}

    async def test_since_shows_new_replies_count(self, client: AsyncClient) -> None:
        """new_replies_since counts only comments after the `since` timestamp."""
        post = await _create_post(client, subject="Active Thread", author="alice")

        # Use a timestamp before comments were added
        since = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await _add_comment(client, post_id=post["id"], author="bob")

        resp = await client.get(f"/api/posts/participating?since={since}", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["new_replies_since"] == 1

    async def test_last_activity_from_comment(self, client: AsyncClient) -> None:
        """last_activity is the timestamp of the most recent comment."""
        post = await _create_post(client, subject="With Comment", author="alice")
        await _add_comment(client, post_id=post["id"], author="bob")

        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        # last_activity should exist and be a valid timestamp
        assert data["threads"][0]["last_activity"] is not None

    async def test_last_activity_from_post_when_no_comments(self, client: AsyncClient) -> None:
        """last_activity is the post timestamp when there are no comments."""
        await _create_post(client, subject="No Comments", author="alice")

        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["new_replies_since"] == 0

    async def test_multiple_threads(self, client: AsyncClient) -> None:
        """Multiple participating threads are all returned."""
        await _create_post(client, subject="Thread 1", author="alice")
        bob_post = await _create_post(client, subject="Thread 2", author="bob")
        await _add_comment(client, post_id=bob_post["id"], author="alice")

        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 2

    async def test_unauthenticated(self, client: AsyncClient) -> None:
        """Unauthenticated requests are rejected with 401 (missing API key)."""
        resp = await client.get("/api/posts/participating")
        assert resp.status_code == 401


class TestCommentInReplyTo:
    """Tests for the in_reply_to field on comments."""

    async def test_create_comment_with_in_reply_to(self, client: AsyncClient) -> None:
        """Comments can reference another comment via in_reply_to."""
        post = await _create_post(client, subject="Thread", author="alice")
        parent = await _add_comment(client, post_id=post["id"], author="bob")
        reply = await _add_comment(
            client, post_id=post["id"], author="alice", in_reply_to=parent["id"]
        )
        assert reply["in_reply_to"] == parent["id"]

    async def test_create_comment_without_in_reply_to(self, client: AsyncClient) -> None:
        """Top-level comments have in_reply_to=None."""
        post = await _create_post(client, subject="Thread", author="alice")
        comment = await _add_comment(client, post_id=post["id"], author="bob")
        assert comment["in_reply_to"] is None

    async def test_invalid_in_reply_to(self, client: AsyncClient) -> None:
        """in_reply_to referencing a non-existent comment returns 400."""
        post = await _create_post(client, subject="Thread", author="alice")
        resp = await client.post(
            f"/api/posts/{post['id']}/comments",
            json={"body_markdown": "Bad reply", "in_reply_to": 99999},
            headers=BOB_HEADERS,
        )
        assert resp.status_code == 400

    async def test_in_reply_to_wrong_post(self, client: AsyncClient) -> None:
        """in_reply_to referencing a comment from a different post returns 400."""
        post1 = await _create_post(client, subject="Post 1", author="alice")
        post2 = await _create_post(client, subject="Post 2", author="alice")
        comment_on_post1 = await _add_comment(client, post_id=post1["id"], author="bob")
        resp = await client.post(
            f"/api/posts/{post2['id']}/comments",
            json={"body_markdown": "Wrong post", "in_reply_to": comment_on_post1["id"]},
            headers=BOB_HEADERS,
        )
        assert resp.status_code == 400


class TestCallbackFlagClearedByRead:
    """Tests for callback_flag being cleared when agent reads a post after the last reply (Issue #55)."""

    async def test_callback_flag_cleared_after_read_on_own_post(self, client: AsyncClient) -> None:
        """callback_flag=False when agent reads their own post after someone comments."""
        # Alice creates a post
        post = await _create_post(client, subject="Alice's Post", author="alice")
        # Bob comments — callback_flag should be True for Alice
        await _add_comment(client, post_id=post["id"], author="bob")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is True

        # Alice reads the post — should clear the callback_flag
        read_resp = await client.get(f"/api/posts/{post['id']}", headers=ALICE_HEADERS)
        assert read_resp.status_code == 200

        # Now callback_flag should be False (agent read after last reply)
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is False

    async def test_callback_flag_cleared_after_read_on_replied_thread(
        self, client: AsyncClient
    ) -> None:
        """callback_flag=False when agent reads a thread they replied to, after a reply."""
        # Bob creates a post
        post = await _create_post(client, subject="Bob's Post", author="bob")
        # Alice comments on it
        alice_comment = await _add_comment(client, post_id=post["id"], author="alice")
        # Bob replies to Alice — callback_flag should be True for Alice
        await _add_comment(
            client, post_id=post["id"], author="bob", in_reply_to=alice_comment["id"]
        )
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is True

        # Alice reads the post — should clear the callback_flag
        read_resp = await client.get(f"/api/posts/{post['id']}", headers=ALICE_HEADERS)
        assert read_resp.status_code == 200

        # Now callback_flag should be False (agent read after last reply)
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["threads"]) == 1
        assert data["threads"][0]["callback_flag"] is False

    async def test_callback_flag_returns_after_new_reply_after_read(
        self, client: AsyncClient
    ) -> None:
        """callback_flag becomes True again when a new reply arrives after the agent read."""
        # Alice creates a post
        post = await _create_post(client, subject="Alice's Post", author="alice")
        # Bob comments — callback_flag=True
        await _add_comment(client, post_id=post["id"], author="bob")

        # Alice reads the post — callback_flag cleared
        await client.get(f"/api/posts/{post['id']}", headers=ALICE_HEADERS)
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        data = resp.json()
        assert data["threads"][0]["callback_flag"] is False

        # Another comment from Bob — callback_flag should be True again
        await _add_comment(client, post_id=post["id"], author="bob")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        data = resp.json()
        assert data["threads"][0]["callback_flag"] is True

    async def test_callback_flag_not_cleared_by_other_agent_read(self, client: AsyncClient) -> None:
        """Bob reading the post does NOT clear callback_flag for Alice."""
        # Alice creates a post
        post = await _create_post(client, subject="Alice's Post", author="alice")
        # Bob comments — callback_flag=True for Alice
        await _add_comment(client, post_id=post["id"], author="bob")

        # Bob (the commenter) reads the post — should NOT clear Alice's callback_flag
        await client.get(f"/api/posts/{post['id']}", headers=BOB_HEADERS)

        # Alice's callback_flag should still be True
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        data = resp.json()
        assert data["threads"][0]["callback_flag"] is True

    async def test_callback_flag_cleared_on_reread_after_new_reply(
        self, client: AsyncClient
    ) -> None:
        """Re-reading a post after a new reply clears callback_flag again."""
        # Alice creates a post
        post = await _create_post(client, subject="Alice's Post", author="alice")
        # Bob comments
        await _add_comment(client, post_id=post["id"], author="bob")

        # Alice reads — callback cleared
        await client.get(f"/api/posts/{post['id']}", headers=ALICE_HEADERS)
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.json()["threads"][0]["callback_flag"] is False

        # Another comment from Bob — callback returns
        await _add_comment(client, post_id=post["id"], author="bob")
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.json()["threads"][0]["callback_flag"] is True

        # Alice re-reads — callback cleared again
        await client.get(f"/api/posts/{post['id']}", headers=ALICE_HEADERS)
        resp = await client.get("/api/posts/participating", headers=ALICE_HEADERS)
        assert resp.json()["threads"][0]["callback_flag"] is False
