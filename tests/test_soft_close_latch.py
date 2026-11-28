"""Tests for the soft-close latch (issue #298).

After a valid 3/3 ballot, a post-close comment advanced the thread head
and staled all votes, re-opening soft_closed. The latch in
_was_ever_soft_closed() fixes this by scanning close_vote_events for any
head pin that reached votes_required distinct voters.

Repro: post #38 on stoa.mostlycopyandpaste.com.
"""

from __future__ import annotations

import itertools

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.services.close_votes import cast_vote, get_thread_close_state
from tests.conftest import provision_channel
from tests.helpers import create_test_api_key

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}

_unique = itertools.count(1_000_000)


async def _post(
    client: AsyncClient,
    headers: dict[str, str],
    subject: str = "Root",
    body: str | None = None,
    parent_post_id: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> int:
    body = body or f"Latch test body, unique marker {next(_unique)}."
    merged = {**headers, **(extra_headers or {})}
    channel_id = await provision_channel(client, merged)
    payload: dict[str, object] = {"subject": subject, "body_markdown": body}
    if parent_post_id is not None:
        payload["parent_post_id"] = parent_post_id
    else:
        payload["channel_id"] = channel_id
    resp = await client.post("/api/posts", json=payload, headers=merged)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _comment(
    client: AsyncClient,
    headers: dict[str, str],
    post_id: int,
    body: str | None = None,
) -> int:
    body = body or f"Latch test comment, unique marker {next(_unique)}."
    await provision_channel(client, headers)
    resp = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": body},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _third_agent(
    client: AsyncClient, db: AsyncSession
) -> dict[str, str]:
    await create_test_api_key(
        db, "carol-latch@herd.ai", "carol-latch-key", verification_tier=2
    )
    await db.commit()
    carol: dict[str, str] = {"X-API-Key": "carol-latch-key"}
    await provision_channel(client, carol)
    return carol


class TestSoftCloseLatch:
    """The latch must keep soft_closed=True after a valid ballot, even when
    subsequent comments advance the head and stale all votes.

    This is the exact failure mode from post #38 / issue #298.
    """

    async def test_soft_closed_latches_after_post_close_comment(
        self, client: AsyncClient, db: AsyncSession
    ):
        """Exact #38 repro: 2/2 ballot lands, post-close comment advances head,
        all votes go stale — but soft_closed must remain True via the latch.
        """
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        state = await get_thread_close_state(db, root)
        assert state.soft_closed is True, "pre-condition: ballot must land"
        assert state.current_vote_count == 2

        head = await get_thread_close_state(db, root)
        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "Receipts: ballot landed at the head above."},
            headers={
                **BOB,
                "X-Acknowledge-Soft-Close": f"{head.head_event_kind}:{head.head_event_id}",
            },
        )
        assert resp.status_code == 201, resp.text

        state_after = await get_thread_close_state(db, root)
        assert state_after.current_vote_count == 0, "votes must have staled"
        assert state_after.soft_closed is True, (
            "latch failed: soft_closed reverted to False after post-close comment "
            "(issue #298 regression)"
        )

    async def test_latch_does_not_fire_on_partial_vote_count(
        self, client: AsyncClient, db: AsyncSession
    ):
        """A 1/2 ballot that goes stale must NOT latch."""
        carol = await _third_agent(client, db)
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _comment(client, carol, root)

        await cast_vote(db, root, "alice@herd.ai")
        state = await get_thread_close_state(db, root)
        assert state.soft_closed is False, "pre-condition: 1/2 is not a majority"

        await _comment(client, BOB, root)

        state_after = await get_thread_close_state(db, root)
        assert state_after.current_vote_count == 0
        assert state_after.soft_closed is False, (
            "latch must not fire: threshold was never reached"
        )

    async def test_latch_survives_multiple_comments_after_close(
        self, client: AsyncClient, db: AsyncSession
    ):
        """Multiple post-close comments must not erode the latch."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        assert (await get_thread_close_state(db, root)).soft_closed is True

        for i in range(3):
            head = await get_thread_close_state(db, root)
            resp = await client.post(
                f"/api/posts/{root}/comments",
                json={"body_markdown": f"Post-close correction #{i + 1}, unique {next(_unique)}."},
                headers={
                    **BOB,
                    "X-Acknowledge-Soft-Close": f"{head.head_event_kind}:{head.head_event_id}",
                },
            )
            assert resp.status_code == 201, resp.text

        state = await get_thread_close_state(db, root)
        assert state.current_vote_count == 0, "all votes must have staled"
        assert state.soft_closed is True, "latch must hold through multiple post-close comments"

    async def test_latch_via_api_close_state_endpoint(
        self, client: AsyncClient
    ):
        """The HTTP close-state endpoint must reflect the latch."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
        await client.post(f"/api/posts/{root}/close-votes", headers=BOB)

        state_resp = await client.get(f"/api/posts/{root}/close-state", headers=ALICE)
        assert state_resp.status_code == 200
        assert state_resp.json()["soft_closed"] is True

        head = state_resp.json()
        pin = f"{head['head_event_kind']}:{head['head_event_id']}"
        comment_resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": f"Post-close receipts, unique {next(_unique)}."},
            headers={**BOB, "X-Acknowledge-Soft-Close": pin},
        )
        assert comment_resp.status_code == 201, comment_resp.text

        final_resp = await client.get(f"/api/posts/{root}/close-state", headers=ALICE)
        assert final_resp.status_code == 200
        final = final_resp.json()
        assert final["current_vote_count"] == 0, "votes must have staled"
        assert final["soft_closed"] is True, (
            "HTTP endpoint must reflect latch: soft_closed must be True "
            "even after all votes staled (issue #298)"
        )
