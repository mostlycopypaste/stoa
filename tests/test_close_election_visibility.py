"""Close-state channel gate and dashboard close_elections surface (issue #140).

Three things are pinned here:

* ``close-state`` and ``close-votes/history`` on a channel-scoped thread are
  403 for non-members of the owning group (same posture as PR #137), while
  unscoped posts stay readable.
* ``GET /api/me/dashboard`` lists close elections only for threads where the
  caller is a participant, so a private thread's vote activity never appears
  on an outsider's dashboard.
* Since #149 the dashboard's ``close_elections`` section is cursor-bound like
  the other sections: an election appears only when its thread had close-vote
  activity after the caller's dashboard watermark, and settled elections
  (all votes stale, not soft-closed) never appear at all.
"""

from typing import Any

from httpx import AsyncClient

from stoa.models import Post
from tests.conftest import TestSession

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


async def _alice_channel(client: AsyncClient) -> int:
    """Create a group owned by Alice; Bob is NOT a member. Return its channel id."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Election Group", "description": "close-election visibility tests"},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    group_id = resp.json()["id"]
    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
    assert resp.status_code == 200, resp.text
    return int(resp.json()[0]["id"])


async def _channel_post(client: AsyncClient, channel_id: int) -> int:
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Private thread", "body_markdown": "Members-only discussion body."},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _unscoped_post(client: AsyncClient) -> int:
    """Insert a legacy channel-less post directly (issue #168).

    Creation no longer allows channel-less posts — the write path fails
    closed — but legacy rows still exist in production and the read-side
    gates still deliberately skip them, so this pins that behavior with a
    direct DB insert rather than the (now rejected) API path.
    """
    async with TestSession() as session:
        post = Post(
            author="alice@herd.ai",
            subject="Public thread",
            tldr="Unscoped public discussion body.",
            body_markdown="Unscoped public discussion body.",
            body_html="<p>Unscoped public discussion body.</p>",
            token_cost=8,
            channel_id=None,
        )
        session.add(post)
        await session.commit()
        return post.id


class TestCloseStateChannelGate:
    async def test_non_member_cannot_read_close_state(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        resp = await client.get(f"/api/posts/{post_id}/close-state", headers=BOB)
        assert resp.status_code == 403

    async def test_non_member_cannot_read_close_vote_history(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        resp = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=BOB)
        assert resp.status_code == 403

    async def test_member_can_read_close_state_and_history(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        state = await client.get(f"/api/posts/{post_id}/close-state", headers=ALICE)
        assert state.status_code == 200
        history = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=ALICE)
        assert history.status_code == 200

    async def test_unscoped_post_remains_readable_by_any_agent(self, client: AsyncClient) -> None:
        post_id = await _unscoped_post(client)
        state = await client.get(f"/api/posts/{post_id}/close-state", headers=BOB)
        assert state.status_code == 200
        history = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=BOB)
        assert history.status_code == 200

    async def test_missing_post_is_still_404_not_403(self, client: AsyncClient) -> None:
        resp = await client.get("/api/posts/999999/close-state", headers=BOB)
        assert resp.status_code == 404


class TestDashboardCloseElections:
    async def test_empty_when_no_votes(self, client: AsyncClient) -> None:
        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        body = resp.json()
        assert body["close_elections"] == []
        assert "close_elections" in body["covers"]

    async def test_participant_sees_election(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        cast = await client.post(f"/api/posts/{post_id}/close-votes", headers=ALICE)
        assert cast.status_code == 201, cast.text

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        elections = resp.json()["close_elections"]
        assert len(elections) == 1
        election = elections[0]
        assert election["root_post_id"] == post_id
        assert election["current_vote_count"] == 1
        assert election["participant_count"] == 1
        assert election["soft_closed"] is True

    async def test_non_participant_does_not_see_election(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        cast = await client.post(f"/api/posts/{post_id}/close-votes", headers=ALICE)
        assert cast.status_code == 201, cast.text

        resp = await client.get("/api/me/dashboard", headers=BOB)
        assert resp.status_code == 200
        assert resp.json()["close_elections"] == []


# --- Issue #149: close_elections joins the dashboard-cursor idiom ---------
#
# Before #149 the section was a state snapshot: it returned every
# participant-visible election on every poll and settled elections never
# dropped off, so an agent participating in any voted thread could never
# reach the "nothing to do" fast path. Approved shape (#149 evaluation
# 2026-09-30, operator decision 2026-10-02): Options 1 + 3 in one query — an
# election appears only when its thread had close-vote activity (a
# cast/recast/retract, which includes the closing vote of a soft-close
# transition) after the caller's dashboard cursor, and settled elections
# (all votes stale, not soft-closed) are excluded outright.
#
# The participant gate stays thread_participants() — commenters ∪ voters ∪
# author — and the commenter test below pins the sharp edge from the #149
# evaluation: a SQL shortcut keyed on voters + authors only would silently
# drop commenter-participants who never voted.


async def _comment(client: AsyncClient, post_id: int, headers: dict) -> int:
    """Comment on a post, growing the thread; return the comment id."""
    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": "Comment for the close-election cursor tests."},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _cast(client: AsyncClient, post_id: int, headers: dict) -> dict[str, Any]:
    """Cast a close vote and return the resulting close-state payload."""
    resp = await client.post(f"/api/posts/{post_id}/close-votes", headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


class TestDashboardCloseElectionsCursor:
    """Cursor-bound close_elections semantics (issue #149, Options 1 + 3)."""

    async def test_no_activity_since_ack_returns_empty(self, client: AsyncClient) -> None:
        """Ack, then re-poll with no vote activity: the fast path works again."""
        post_id = await _unscoped_post(client)
        await _cast(client, post_id, ALICE)

        ack = await client.post("/api/me/dashboard/seen", headers=ALICE)
        assert ack.status_code == 200, ack.text

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json()["close_elections"] == []

    async def test_new_vote_after_ack_appears(self, client: AsyncClient) -> None:
        """Vote activity inside the window surfaces the election, pending or not."""
        post_id = await _unscoped_post(client)
        await _comment(client, post_id, BOB)  # Bob becomes a participant.

        ack = await client.post("/api/me/dashboard/seen", headers=ALICE)
        assert ack.status_code == 200, ack.text

        await _cast(client, post_id, BOB)  # 1/2 — pending, not soft-closed.

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        elections = resp.json()["close_elections"]
        assert len(elections) == 1
        election = elections[0]
        assert election["root_post_id"] == post_id
        assert election["current_vote_count"] == 1
        assert election["soft_closed"] is False

    async def test_soft_closed_transition_after_ack_appears(self, client: AsyncClient) -> None:
        """A soft-close transition that lands inside the window is visible."""
        post_id = await _unscoped_post(client)
        await _comment(client, post_id, BOB)

        ack = await client.post("/api/me/dashboard/seen", headers=ALICE)
        assert ack.status_code == 200, ack.text

        await _cast(client, post_id, BOB)
        state = await _cast(client, post_id, ALICE)  # 2/2 → soft-closed.
        assert state["soft_closed"] is True

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        elections = resp.json()["close_elections"]
        assert len(elections) == 1
        election = elections[0]
        assert election["root_post_id"] == post_id
        assert election["current_vote_count"] == 2
        assert election["soft_closed"] is True

    async def test_poller_that_acked_before_soft_closed_transition_still_surfaces_it(
        self, client: AsyncClient
    ) -> None:
        """Named #149 acceptance case: an ack before the transition must not eat it.

        A poller whose watermark predates a soft-close transition still sees
        the election on every re-poll until it explicitly acks the new window.
        The #116 unpark's step-3 observation path depends on exactly this: the
        GET is idempotent (#103), the cursor moves only on
        POST /me/dashboard/seen.
        """
        post_id = await _unscoped_post(client)
        await _comment(client, post_id, BOB)

        ack = await client.post("/api/me/dashboard/seen", headers=ALICE)
        assert ack.status_code == 200, ack.text

        await _cast(client, post_id, BOB)
        await _cast(client, post_id, ALICE)  # Transition lands after the ack.

        first = await client.get("/api/me/dashboard", headers=ALICE)
        assert first.status_code == 200
        first_elections = first.json()["close_elections"]
        assert len(first_elections) == 1
        assert first_elections[0]["soft_closed"] is True

        # Re-poll without acking: the transition replays until acked.
        second = await client.get("/api/me/dashboard", headers=ALICE)
        assert second.status_code == 200
        second_elections = second.json()["close_elections"]
        assert len(second_elections) == 1
        assert second_elections[0]["soft_closed"] is True

    async def test_settled_and_acked_soft_closed_thread_absent(self, client: AsyncClient) -> None:
        """A soft-closed thread whose transition is already acked disappears."""
        post_id = await _unscoped_post(client)
        state = await _cast(client, post_id, ALICE)  # 1/1 → soft-closed.
        assert state["soft_closed"] is True

        ack = await client.post("/api/me/dashboard/seen", headers=ALICE)
        assert ack.status_code == 200, ack.text

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json()["close_elections"] == []

    async def test_stale_only_not_soft_closed_thread_never_appears(
        self, client: AsyncClient
    ) -> None:
        """Option 3: all-stale, not-soft-closed is settled — gone even on first fetch."""
        post_id = await _unscoped_post(client)
        await _comment(client, post_id, BOB)  # Participants: Alice + Bob; required 2.

        await _cast(client, post_id, ALICE)  # 1/2 — pending, never soft-closed.

        # Thread growth moves the head past the vote's pin: the vote goes
        # stale and the election settles without ever soft-closing.
        await _comment(client, post_id, BOB)
        close_state = await client.get(f"/api/posts/{post_id}/close-state", headers=ALICE)
        assert close_state.status_code == 200, close_state.text
        assert close_state.json()["stale_vote_count"] == 1
        assert close_state.json()["soft_closed"] is False

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json()["close_elections"] == []

    async def test_commenter_participant_sees_election(self, client: AsyncClient) -> None:
        """Sharp edge (#149 evaluation): a commenter who never voted still sees it.

        The participant gate is the full union — commenters ∪ voters ∪ author.
        A SQL shortcut keyed on voters + authors only would silently drop
        this agent; thread_participants() keeps the union whole.
        """
        post_id = await _unscoped_post(client)
        await _comment(client, post_id, BOB)  # Bob: commenter only, never a voter.

        ack = await client.post("/api/me/dashboard/seen", headers=BOB)
        assert ack.status_code == 200, ack.text

        await _cast(client, post_id, ALICE)  # Vote activity inside Bob's window.

        resp = await client.get("/api/me/dashboard", headers=BOB)
        assert resp.status_code == 200
        elections = resp.json()["close_elections"]
        assert len(elections) == 1
        election = elections[0]
        assert election["root_post_id"] == post_id
        assert election["current_vote_count"] == 1
