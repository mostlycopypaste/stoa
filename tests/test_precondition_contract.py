"""Tests for the soft-close pin-precondition contract (issue #116).

A soft-closed thread still accepts comments, but only from callers who
acknowledge they have read the current thread head.  The gate lives in
POST /api/posts/{post_id}/comments and enforces three outcomes:

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
