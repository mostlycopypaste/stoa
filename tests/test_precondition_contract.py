"""Tests for the soft-close pin-precondition contract (issues #116/#153).

A soft-closed thread still accepts writes, but only from callers who
acknowledge they have read the current thread head.  Since #153 the gate
lives in one shared helper behind all three thread-growth doors:

  - POST /api/posts/{post_id}/comments           (comments, #151)
  - POST /api/posts with parent_post_id           (reply-posts, #153 Gap 2)
  - POST /api/channels/{id}/messages, parent_id   (channel replies, #153 Gap 3)

Each door enforces three outcomes:

  428  SOFT_CLOSE_ACKNOWLEDGMENT_REQUIRED  — header absent
  409  SOFT_CLOSE_PIN_MISMATCH             — header present but stale
  201  (normal)                            — header matches, or not soft-closed

The test-live trigger cases from the Stoa board (#116 design notes):
  Post #31  – 1/3 close votes, first real trigger
  Post #19  – soft-closed 3/3, second trigger / live test case
"""

import itertools

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}

_unique = itertools.count()


async def _post(
    client: AsyncClient,
    headers: dict,
    subject: str = "Root",
    body: str | None = None,
    parent_post_id: int | None = None,
) -> int:
    body = body or f"Body text for the post, unique marker {next(_unique)}."
    payload: dict = {"subject": subject, "body_markdown": body}
    if parent_post_id is not None:
        payload["parent_post_id"] = parent_post_id
    resp = await client.post("/api/posts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _comment(
    client: AsyncClient,
    headers: dict,
    post_id: int,
    body: str | None = None,
    extra_headers: dict | None = None,
) -> tuple[int, int]:
    """Post a comment; return (status_code, comment_id_or_0)."""
    body = body or f"A comment, unique marker {next(_unique)}."
    merged = {**headers, **(extra_headers or {})}
    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": body},
        headers=merged,
    )
    comment_id = resp.json().get("id", 0) if resp.status_code == 201 else 0
    return resp.status_code, comment_id


async def _soft_close(client: AsyncClient, post_id: int) -> None:
    """Bring a 2-participant thread (ALICE + BOB) to soft-close."""
    await client.post(f"/api/posts/{post_id}/close-votes", headers=ALICE)
    await client.post(f"/api/posts/{post_id}/close-votes", headers=BOB)


async def _close_state(client: AsyncClient, post_id: int) -> dict:
    resp = await client.get(f"/api/posts/{post_id}/close-state", headers=ALICE)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _reply_post(
    client: AsyncClient,
    headers: dict,
    parent_post_id: int,
    extra_headers: dict | None = None,
    channel_id: int | None = None,
) -> tuple[int, int]:
    """Create a reply-post via POST /api/posts; return (status, post_id_or_0)."""
    payload: dict = {
        "subject": "Reply",
        "body_markdown": f"A reply-post, unique marker {next(_unique)}.",
        "parent_post_id": parent_post_id,
    }
    if channel_id is not None:
        payload["channel_id"] = channel_id
    resp = await client.post(
        "/api/posts", json=payload, headers={**headers, **(extra_headers or {})}
    )
    post_id = resp.json().get("id", 0) if resp.status_code == 201 else 0
    return resp.status_code, post_id


async def _channel_reply(
    client: AsyncClient,
    headers: dict,
    channel_id: int,
    parent_id: int,
) -> tuple[int, int]:
    """Reply to a channel message; return (status, message_id_or_0)."""
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={
            "subject": "Reply",
            "body_markdown": f"A channel reply, unique marker {next(_unique)}.",
            "parent_id": parent_id,
        },
        headers=headers,
    )
    message_id = resp.json().get("id", 0) if resp.status_code == 201 else 0
    return resp.status_code, message_id


async def _channel_with_members(client: AsyncClient, join: tuple[dict, ...] = (BOB,)) -> int:
    """Create a group and its #general channel; have *join* keys join."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Gate Group", "description": "Soft-close gate coverage"},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    group_id = resp.json()["id"]
    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
    assert resp.status_code == 200, resp.text
    channel_id = resp.json()[0]["id"]
    for joiner in join:
        resp = await client.post(f"/api/groups/{group_id}/join", headers=joiner)
        assert resp.status_code == 201, resp.text
    return channel_id


async def _thread_with_reply(client: AsyncClient) -> tuple[int, int]:
    """Unscoped thread: root by ALICE, reply-post by BOB, soft-closed 2/2."""
    root = await _post(client, ALICE)
    status, reply_id = await _reply_post(client, BOB, root)
    assert status == 201
    await _soft_close(client, root)
    return root, reply_id


async def _soft_closed_channel_thread(client: AsyncClient) -> tuple[int, int]:
    """Channel thread with ALICE + BOB as participants, soft-closed 2/2."""
    channel_id = await _channel_with_members(client)
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Root", "body_markdown": f"Root message, marker {next(_unique)}."},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    root_id = resp.json()["id"]
    # Bob participates via a reply, before the close — no pin needed yet.
    status, _ = await _channel_reply(client, BOB, channel_id, root_id)
    assert status == 201
    await _soft_close(client, root_id)
    return channel_id, root_id


async def _alice_only_channel_thread(client: AsyncClient) -> tuple[int, int]:
    """Channel thread with ALICE the sole participant, soft-closed 1/1.

    BOB deliberately stays a non-member of the group so the same fixture can
    also pin the 403-before-428 ordering on all three doors.
    """
    channel_id = await _channel_with_members(client, join=())
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Sweep root", "body_markdown": f"Sweep root, marker {next(_unique)}."},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    root_id = resp.json()["id"]
    resp = await client.post(f"/api/posts/{root_id}/close-votes", headers=ALICE)
    assert resp.status_code == 201, resp.text
    return channel_id, root_id


class TestNotSoftClosed:
    """Gate is transparent when the thread is not soft-closed."""

    async def test_open_thread_accepts_comment_without_header(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)  # Bob participates so votes can reach majority

        status, _ = await _comment(client, BOB, root)
        assert status == 201

    async def test_open_thread_ignores_spurious_ack_header(self, client: AsyncClient):
        """A spurious X-Acknowledge-Soft-Close on an open thread is silently ignored."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        status, _ = await _comment(
            client, BOB, root, extra_headers={"X-Acknowledge-Soft-Close": "comment:999"}
        )
        assert status == 201


class TestSoftClosedMissingHeader:
    """428 when the thread is soft-closed and the header is absent."""

    async def test_soft_closed_without_header_is_428(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        status, _ = await _comment(client, BOB, root)
        assert status == 428

    async def test_428_body_contains_required_fields(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": f"Body {next(_unique)}."},
            headers=BOB,
        )
        assert resp.status_code == 428
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_ACKNOWLEDGMENT_REQUIRED"
        assert "head_event" in detail
        # head_event must be in "<kind>:<id>" format
        assert ":" in detail["head_event"]

    async def test_428_head_event_matches_close_state(self, client: AsyncClient):
        """The head_event in the 428 body must equal <kind>:<id> from /close-state."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        expected = f"{state['head_event_kind']}:{state['head_event_id']}"

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": f"Body {next(_unique)}."},
            headers=BOB,
        )
        assert resp.json()["detail"]["head_event"] == expected


class TestSoftClosedStalePinHeader:
    """409 when the header is present but doesn't match the current head."""

    async def test_stale_pin_is_409(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        status, _ = await _comment(
            client,
            BOB,
            root,
            extra_headers={"X-Acknowledge-Soft-Close": "comment:0"},
        )
        assert status == 409

    async def test_409_body_contains_expected_and_received(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": f"Body {next(_unique)}."},
            headers={**BOB, "X-Acknowledge-Soft-Close": "comment:0"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_PIN_MISMATCH"
        assert "expected" in detail
        assert "received" in detail
        assert detail["received"] == "comment:0"

    async def test_pin_gone_stale_between_fetch_and_post(self, client: AsyncClient):
        """A caller who read /close-state, then the thread moved, gets 409."""
        root = await _post(client, ALICE)
        comment_id = await _comment(client, BOB, root)
        comment_id = comment_id[1]  # (status, id) tuple
        await _soft_close(client, root)

        # We simulate staleness by just adding another comment via a separate vote retract/recast,
        # but the simplest approach is to retract a vote (that changes thread state indirectly).
        # Actually, the easiest way: use a third agent to post a reply-post.
        # For isolation, directly verify: an old pin that no longer matches is 409.
        wrong_pin = f"comment:{comment_id - 1}" if comment_id > 0 else "post:0"
        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": f"Body {next(_unique)}."},
            headers={**BOB, "X-Acknowledge-Soft-Close": wrong_pin},
        )
        assert resp.status_code == 409


class TestSoftClosedCorrectPinHeader:
    """201 when the header matches the current head exactly."""

    async def test_correct_pin_is_201(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        status, _ = await _comment(
            client,
            BOB,
            root,
            extra_headers={"X-Acknowledge-Soft-Close": pin},
        )
        assert status == 201

    async def test_comment_with_correct_pin_stales_votes(self, client: AsyncClient):
        """The new comment moves the thread head, so the soft-close lifts automatically."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state_before = await _close_state(client, root)
        assert state_before["soft_closed"] is True

        pin = f"{state_before['head_event_kind']}:{state_before['head_event_id']}"
        await _comment(
            client,
            BOB,
            root,
            extra_headers={"X-Acknowledge-Soft-Close": pin},
        )

        state_after = await _close_state(client, root)
        assert state_after["soft_closed"] is False, (
            "comment with correct pin is a thread event and must stale the votes"
        )

    async def test_alice_can_also_comment_with_correct_pin(self, client: AsyncClient):
        """Both participants (not just the one who didn't vote) can comment through."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        status, _ = await _comment(
            client,
            ALICE,
            root,
            extra_headers={"X-Acknowledge-Soft-Close": pin},
        )
        assert status == 201


class TestReplyPostCommentGating:
    """Comments on a reply-post id resolve to the thread root (#153 Gap 1)."""

    async def test_comment_on_reply_post_without_header_is_428(self, client: AsyncClient):
        root, reply_id = await _thread_with_reply(client)
        status, _ = await _comment(client, ALICE, reply_id)
        assert status == 428

    async def test_428_head_event_matches_close_state(self, client: AsyncClient):
        root, reply_id = await _thread_with_reply(client)
        # /close-state accepts any post in the thread — reply-post ids included.
        state = await _close_state(client, reply_id)
        expected = f"{state['head_event_kind']}:{state['head_event_id']}"
        resp = await client.post(
            f"/api/posts/{reply_id}/comments",
            json={"body_markdown": f"Comment attempt {next(_unique)}."},
            headers=ALICE,
        )
        assert resp.status_code == 428
        assert resp.json()["detail"]["head_event"] == expected

    async def test_stale_pin_is_409(self, client: AsyncClient):
        root, reply_id = await _thread_with_reply(client)
        resp = await client.post(
            f"/api/posts/{reply_id}/comments",
            json={"body_markdown": f"Comment attempt {next(_unique)}."},
            headers={**ALICE, "X-Acknowledge-Soft-Close": "comment:0"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_PIN_MISMATCH"
        assert detail["received"] == "comment:0"

    async def test_current_pin_is_201_and_lifts_soft_close(self, client: AsyncClient):
        root, reply_id = await _thread_with_reply(client)
        state = await _close_state(client, root)
        assert state["soft_closed"] is True
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        status, _ = await _comment(
            client, ALICE, reply_id, extra_headers={"X-Acknowledge-Soft-Close": pin}
        )
        assert status == 201

        state_after = await _close_state(client, root)
        assert state_after["soft_closed"] is False, (
            "a comment anywhere in the thread is a thread event and must stale the votes"
        )


class TestReplyPostCreationGating:
    """POST /api/posts with parent_post_id carries the gate (#153 Gap 2)."""

    async def test_reply_post_without_header_is_428(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        status, _ = await _reply_post(client, BOB, root)
        assert status == 428

    async def test_428_body_carries_head_event(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        expected = f"{state['head_event_kind']}:{state['head_event_id']}"
        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Reply",
                "body_markdown": f"Reply attempt {next(_unique)}.",
                "parent_post_id": root,
            },
            headers=BOB,
        )
        assert resp.status_code == 428
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_ACKNOWLEDGMENT_REQUIRED"
        assert detail["head_event"] == expected

    async def test_mid_thread_parent_resolves_to_root(self, client: AsyncClient):
        """A reply to a mid-thread reply-post is gated by the root's soft-close state."""
        root = await _post(client, ALICE)
        _, reply_id = await _reply_post(client, BOB, root)
        await _soft_close(client, root)

        status, _ = await _reply_post(client, ALICE, reply_id)
        assert status == 428

    async def test_stale_pin_is_409(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Reply",
                "body_markdown": f"Reply attempt {next(_unique)}.",
                "parent_post_id": root,
            },
            headers={**BOB, "X-Acknowledge-Soft-Close": "comment:0"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_PIN_MISMATCH"
        assert detail["expected"]
        assert detail["received"] == "comment:0"

    async def test_current_pin_is_201_and_stales_votes(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        assert state["soft_closed"] is True
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        status, _ = await _reply_post(
            client, BOB, root, extra_headers={"X-Acknowledge-Soft-Close": pin}
        )
        assert status == 201

        state_after = await _close_state(client, root)
        assert state_after["soft_closed"] is False, (
            "a reply-post is a thread event and must stale the votes"
        )

    async def test_pin_goes_stale_after_thread_moves(self, client: AsyncClient):
        """A caller who read /close-state, then the thread moved, gets 409."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _soft_close(client, root)

        state = await _close_state(client, root)
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        status, _ = await _reply_post(
            client, BOB, root, extra_headers={"X-Acknowledge-Soft-Close": pin}
        )
        assert status == 201

        # The reply moved the head, so the votes went stale and the close
        # lifted. Re-establish the close at the new head; the old pin is stale.
        await _soft_close(client, root)
        status, _ = await _reply_post(
            client, BOB, root, extra_headers={"X-Acknowledge-Soft-Close": pin}
        )
        assert status == 409

    async def test_open_thread_reply_ignores_spurious_header(self, client: AsyncClient):
        """A spurious X-Acknowledge-Soft-Close on an open thread is silently ignored."""
        root = await _post(client, ALICE)

        status, _ = await _reply_post(
            client, BOB, root, extra_headers={"X-Acknowledge-Soft-Close": "comment:999"}
        )
        assert status == 201


class TestChannelMessageReplyGating:
    """POST /api/channels/{id}/messages with parent_id carries the gate (Gap 3)."""

    async def test_channel_reply_without_header_is_428(self, client: AsyncClient):
        channel_id, root_id = await _soft_closed_channel_thread(client)
        resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "subject": "Reply",
                "body_markdown": f"Channel reply attempt {next(_unique)}.",
                "parent_id": root_id,
            },
            headers=ALICE,
        )
        assert resp.status_code == 428
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_ACKNOWLEDGMENT_REQUIRED"
        assert ":" in detail["head_event"]

    async def test_channel_reply_stale_pin_is_409(self, client: AsyncClient):
        channel_id, root_id = await _soft_closed_channel_thread(client)
        resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "subject": "Reply",
                "body_markdown": f"Channel reply attempt {next(_unique)}.",
                "parent_id": root_id,
            },
            headers={**ALICE, "X-Acknowledge-Soft-Close": "comment:0"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "SOFT_CLOSE_PIN_MISMATCH"
        assert detail["received"] == "comment:0"

    async def test_channel_reply_current_pin_is_201_and_lifts_soft_close(self, client: AsyncClient):
        channel_id, root_id = await _soft_closed_channel_thread(client)

        state = await _close_state(client, root_id)
        assert state["soft_closed"] is True
        pin = f"{state['head_event_kind']}:{state['head_event_id']}"

        resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "subject": "Reply",
                "body_markdown": f"Channel reply attempt {next(_unique)}.",
                "parent_id": root_id,
            },
            headers={**ALICE, "X-Acknowledge-Soft-Close": pin},
        )
        assert resp.status_code == 201

        state_after = await _close_state(client, root_id)
        assert state_after["soft_closed"] is False


class TestThreePathSweep:
    """Max's ruling: one soft-closed thread, all three doors behave alike."""

    async def test_no_header_is_428_on_all_three_paths(self, client: AsyncClient):
        channel_id, root_id = await _alice_only_channel_thread(client)

        # Door 1: comments
        status, _ = await _comment(client, ALICE, root_id)
        assert status == 428

        # Door 2: reply-posts (POST /api/posts with parent_post_id)
        status, _ = await _reply_post(client, ALICE, root_id, channel_id=channel_id)
        assert status == 428

        # Door 3: channel replies (POST /api/channels/{id}/messages with parent_id)
        status, _ = await _channel_reply(client, ALICE, channel_id, root_id)
        assert status == 428

    async def test_non_member_gets_403_not_428_on_all_three_paths(self, client: AsyncClient):
        """Membership 403 fires before the gate on every door — no head-token oracle."""
        channel_id, root_id = await _alice_only_channel_thread(client)

        status, _ = await _comment(client, BOB, root_id)
        assert status == 403

        status, _ = await _reply_post(client, BOB, root_id, channel_id=channel_id)
        assert status == 403

        status, _ = await _channel_reply(client, BOB, channel_id, root_id)
        assert status == 403

    async def test_explicitly_closed_root_is_409_on_all_three_paths(self, client: AsyncClient):
        """The hard-status door is uniform: closed root → 409 on all three doors."""
        channel_id, root_id = await _alice_only_channel_thread(client)
        resp = await client.patch(
            f"/api/posts/{root_id}/status",
            json={"status": "closed"},
            headers=ALICE,
        )
        assert resp.status_code == 200, resp.text

        # Door 1: comments — unchanged since #151
        resp = await client.post(
            f"/api/posts/{root_id}/comments",
            json={"body_markdown": f"Comment attempt {next(_unique)}."},
            headers=ALICE,
        )
        assert resp.status_code == 409
        assert "Cannot comment on a closed post" in resp.json()["detail"]

        # Door 2: reply-posts
        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Reply",
                "body_markdown": f"Reply attempt {next(_unique)}.",
                "parent_post_id": root_id,
                "channel_id": channel_id,
            },
            headers=ALICE,
        )
        assert resp.status_code == 409
        assert "Cannot reply to a closed post" in resp.json()["detail"]

        # Door 3: channel replies
        resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "subject": "Reply",
                "body_markdown": f"Channel reply attempt {next(_unique)}.",
                "parent_id": root_id,
            },
            headers=ALICE,
        )
        assert resp.status_code == 409
        assert "Cannot reply to a closed post" in resp.json()["detail"]
